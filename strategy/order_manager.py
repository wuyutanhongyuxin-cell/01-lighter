"""
订单管理器

管理套利交易的订单生命周期:
  1. 在 01 下 Post-Only Maker 单
  2. 轮询检测成交 (01 没有 WebSocket)
  3. 01 成交后在 Lighter 下 Taker 对冲单
  4. 等待 Lighter 成交确认
"""

import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

from exchanges.o1_client import O1ExchangeClient
from exchanges.lighter_client import LighterClient
from strategy.position_tracker import PositionTracker
from strategy.data_logger import DataLogger

logger = logging.getLogger("arbitrage.orders")


class OrderManager:
    """套利订单管理器"""

    def __init__(
        self,
        o1_client: O1ExchangeClient,
        lighter_client: LighterClient,
        position_tracker: PositionTracker,
        data_logger: DataLogger,
        o1_market_id: int,
        lighter_market_id: int,
        order_quantity: Decimal,
        fill_timeout: int = 5,
        o1_tick_size: Decimal = Decimal("10"),
    ):
        self.o1 = o1_client
        self.lighter = lighter_client
        self.positions = position_tracker
        self.data_logger = data_logger

        self.o1_market_id = o1_market_id
        self.lighter_market_id = lighter_market_id
        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.o1_tick_size = o1_tick_size

        # 状态
        self._executing = False

    @property
    def is_busy(self) -> bool:
        return self._executing

    async def execute_long_o1(
        self,
        o1_ask: Decimal,
        lighter_bid: Decimal,
    ) -> Optional[dict]:
        """
        执行做多01套利:
          1. 01 Post-Only BUY (ask - tick_size, 确保是 Maker)
          2. 等待01成交 (轮询/尝试撤单检测)
          3. 01成交 → Lighter SELL Taker

        Returns:
            dict = 交易详情 (成功)
            None = 未成交或失败
        """
        if self._executing:
            logger.warning("上一笔套利尚未完成, 跳过")
            return False

        self._executing = True
        try:
            return await self._execute_arb(
                direction="long_01",
                o1_side="buy",
                o1_price=o1_ask - self.o1_tick_size,
                lighter_side="sell",
            )
        finally:
            self._executing = False

    async def execute_short_o1(
        self,
        o1_bid: Decimal,
        lighter_ask: Decimal,
    ) -> Optional[dict]:
        """
        执行做空01套利:
          1. 01 Post-Only SELL (bid + tick_size, 确保是 Maker)
          2. 等待01成交
          3. 01成交 → Lighter BUY Taker
        """
        if self._executing:
            logger.warning("上一笔套利尚未完成, 跳过")
            return False

        self._executing = True
        try:
            return await self._execute_arb(
                direction="short_01",
                o1_side="sell",
                o1_price=o1_bid + self.o1_tick_size,
                lighter_side="buy",
            )
        finally:
            self._executing = False

    async def _execute_arb(
        self,
        direction: str,
        o1_side: str,
        o1_price: Decimal,
        lighter_side: str,
    ) -> Optional[dict]:
        """
        核心套利执行流程

        Phase 1: 01 Maker 单
        Phase 2: 轮询等待成交
        Phase 3: Lighter Taker 对冲
        """
        logger.info(
            f"=== 开始套利: {direction} | "
            f"01 {o1_side}@{o1_price} → Lighter {lighter_side} ==="
        )

        # ===== Phase 1: 在01下 Post-Only 单 =====
        try:
            o1_result = await self.o1.place_order(
                market_id=self.o1_market_id,
                side=o1_side,
                price=o1_price,
                size=self.order_quantity,
                order_type="post_only",
            )
            order_id = o1_result["order_id"]
            self.o1.order_tracker.add_order(
                order_id, o1_side, o1_price, self.order_quantity
            )
        except Exception as e:
            logger.error(f"01 下单失败: {e}")
            return None

        # ===== Phase 2: 等待01成交 (轮询检测) =====
        filled = await self._wait_for_o1_fill(order_id)

        if not filled:
            logger.info(f"01 订单 #{order_id} 超时未成交, 已撤单")
            return None

        logger.info(f"01 订单 #{order_id} 已成交! 立即对冲...")

        # ===== Phase 3: Lighter Taker 对冲 =====
        # 记录对冲时的 Lighter BBO (用于估算实际成交价)
        hedge_bbo = self.lighter.get_ws_bbo(self.lighter_market_id)
        if hedge_bbo:
            logger.info(
                f"对冲时 Lighter BBO: bid={hedge_bbo['best_bid']} "
                f"ask={hedge_bbo['best_ask']}"
            )

        try:
            lighter_result = await self.lighter.place_taker_order(
                market_id=self.lighter_market_id,
                side=lighter_side,
                size=self.order_quantity,
            )
            lighter_submitted_price = lighter_result["price"]
        except Exception as e:
            logger.error(
                f"Lighter 对冲失败! {e} | "
                f"01 端已成交 {o1_side} {self.order_quantity}@{o1_price}, "
                f"仓位可能不平衡!"
            )
            # 即使 Lighter 失败, 也要更新01端仓位
            self.positions.update_o1(o1_side, self.order_quantity)
            return None

        # ===== 估算实际成交价 =====
        # IOC 单在有流动性时成交在 best_bid/ask, 而非提交的限价 (bid×0.998)
        if hedge_bbo:
            if lighter_side == "sell":
                estimated_fill_price = hedge_bbo["best_bid"]
            else:
                estimated_fill_price = hedge_bbo["best_ask"]
        else:
            estimated_fill_price = lighter_submitted_price

        logger.info(
            f"Lighter 提交限价={lighter_submitted_price}, "
            f"估算成交价={estimated_fill_price}"
        )

        # ===== 成功: 更新仓位和日志 =====
        self.positions.record_arb_trade(direction, self.order_quantity)

        # 用估算成交价计算真实价差 (而非提交限价)
        if direction == "long_01":
            spread = estimated_fill_price - o1_price
        else:
            spread = o1_price - estimated_fill_price

        self.data_logger.log_trade(
            direction=direction,
            o1_side=o1_side,
            o1_price=o1_price,
            o1_size=self.order_quantity,
            lighter_side=lighter_side,
            lighter_price=estimated_fill_price,
            lighter_size=self.order_quantity,
            spread_captured=spread,
            o1_position=self.positions.o1_position,
            lighter_position=self.positions.lighter_position,
        )

        logger.info(
            f"=== 套利完成: {direction} | 价差={spread} | "
            f"01={o1_side}@{o1_price} "
            f"Lighter={lighter_side}@{estimated_fill_price} "
            f"(限价={lighter_submitted_price}) ==="
        )
        return {
            "direction": direction,
            "o1_side": o1_side,
            "o1_price": o1_price,
            "lighter_side": lighter_side,
            "lighter_price": estimated_fill_price,
            "size": self.order_quantity,
            "spread": spread,
            "o1_position": self.positions.o1_position,
            "lighter_position": self.positions.lighter_position,
        }

    async def _wait_for_o1_fill(self, order_id: int) -> bool:
        """
        等待01 Maker 单成交 (非破坏性轮询 + 超时撤单)

        策略:
          1. 每 0.5s 通过 orders API 查询订单是否还在 (不撤单, 不破坏订单!)
          2. 订单从挂单列表消失 → 已被 taker 吃掉 → 立即返回 True
          3. 超时后才执行撤单 (唯一的破坏性操作)

        相比旧版: 旧版盲等 fill_timeout 再检查一次, 本版每 0.5s 查一次
        关键: 查询期间订单始终在订单簿上, 有足够时间被成交!
        """
        poll_interval = 0.5
        start = time.time()

        logger.debug(
            f"等待01成交: order_id={order_id}, timeout={self.fill_timeout}s, "
            f"poll={poll_interval}s"
        )

        # 先等 1 秒, 给订单一个初始成交窗口
        await asyncio.sleep(1.0)

        # 轮询: 查询订单是否还在挂单列表 (非破坏性!)
        while time.time() - start < self.fill_timeout:
            try:
                open_orders = await self.o1._get_open_orders_from_api(self.o1_market_id)
                if order_id not in open_orders:
                    # 订单不在挂单列表 → 已成交! (我们没撤过它)
                    elapsed = time.time() - start
                    logger.info(
                        f"01 订单 #{order_id} 已成交! "
                        f"(从挂单列表消失, 检测耗时 {elapsed:.1f}s)"
                    )
                    self.o1.order_tracker.mark_filled(order_id)
                    return True
                else:
                    logger.debug(f"01 订单 #{order_id} 仍在挂单中...")
            except Exception as e:
                # API 查询失败 → 跳过本次, 继续等
                logger.debug(f"查询挂单状态失败 (继续等待): {e}")

            await asyncio.sleep(poll_interval)

        # 超时: 用撤单做最终判断
        elapsed = time.time() - start
        logger.info(f"01 订单 #{order_id} 超时 ({elapsed:.1f}s), 尝试撤单...")
        try:
            cancelled = await self.o1.cancel_order(self.o1_market_id, order_id)
            if cancelled:
                logger.info(f"01 订单 #{order_id} 撤单成功 (未成交)")
                self.o1.order_tracker.mark_cancelled(order_id)
                return False
            else:
                # ORDER_NOT_FOUND = 在超时前一刻成交了
                logger.info(f"01 订单 #{order_id} 超时但已成交!")
                self.o1.order_tracker.mark_filled(order_id)
                return True
        except Exception as e:
            if "ORDER_NOT_FOUND" in str(e):
                logger.info(f"01 订单 #{order_id} 超时但已成交 (异常检测)")
                self.o1.order_tracker.mark_filled(order_id)
                return True
            logger.error(f"超时撤单异常: {e}")
        return False
