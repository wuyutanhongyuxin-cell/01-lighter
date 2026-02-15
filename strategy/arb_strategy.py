"""
主套利策略 (对应原项目的 edgex_arb.py)

01exchange (Maker) ↔ Lighter (Taker) 跨交易所套利
核心逻辑:
  1. 每秒采样两端价差
  2. 价差超过动态阈值时触发
  3. 01 端 Post-Only Maker → 等待成交 → Lighter 端 Taker 对冲
"""

import asyncio
import logging
import signal
import time
from decimal import Decimal
from typing import Optional

from exchanges.o1_client import O1ExchangeClient
from exchanges.lighter_client import LighterClient
from strategy.order_book_manager import OrderBookManager
from strategy.order_manager import OrderManager
from strategy.position_tracker import PositionTracker
from strategy.spread_analyzer import SpreadAnalyzer
from strategy.data_logger import DataLogger

logger = logging.getLogger("arbitrage.strategy")

# 余额检查间隔 (秒)
BALANCE_CHECK_INTERVAL = 10
# 最低余额阈值 (USDC)
MIN_BALANCE = Decimal("10")


class ArbStrategy:
    """01 ↔ Lighter 套利策略"""

    def __init__(
        self,
        o1_client: O1ExchangeClient,
        lighter_client: LighterClient,
        ticker: str = "BTC",
        order_quantity: Decimal = Decimal("0.001"),
        max_position: Decimal = Decimal("0.01"),
        long_threshold: Decimal = Decimal("10"),
        short_threshold: Decimal = Decimal("10"),
        fill_timeout: int = 5,
        warmup_samples: int = 100,
        o1_tick_size: Decimal = Decimal("10"),
    ):
        self.o1 = o1_client
        self.lighter = lighter_client
        self.ticker = ticker

        # 市场 ID (连接后初始化)
        self.o1_market_id: Optional[int] = None
        self.lighter_market_id: Optional[int] = None

        # 子模块
        self.positions = PositionTracker(max_position, order_quantity)
        self.spread = SpreadAnalyzer(
            warmup_samples=warmup_samples,
            long_threshold=long_threshold,
            short_threshold=short_threshold,
        )
        self.data_logger = DataLogger()

        # OrderBookManager 和 OrderManager 在 initialize() 中创建
        self.ob_manager: Optional[OrderBookManager] = None
        self.order_mgr: Optional[OrderManager] = None

        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.o1_tick_size = o1_tick_size

        # 运行状态
        self._stop_flag = False
        self._last_balance_check: float = 0

    async def initialize(self):
        """
        初始化策略:
          1. 连接两端交易所
          2. 解析市场 ID
          3. 启动 Lighter WebSocket
          4. 创建订单管理器
        """
        logger.info("=" * 60)
        logger.info("01 ↔ Lighter 套利策略初始化")
        logger.info("=" * 60)

        # 1. 连接两端
        logger.info("连接 01exchange...")
        await self.o1.connect()

        logger.info("连接 Lighter...")
        await self.lighter.connect()

        # 2. 解析市场 ID
        self.o1_market_id = self.o1.get_market_id(self.ticker)
        self.lighter_market_id = self.lighter.get_market_index(self.ticker)
        logger.info(
            f"市场映射: {self.ticker} → "
            f"01={self.o1_market_id}, Lighter={self.lighter_market_id}"
        )

        # 3. 启动 Lighter WebSocket
        await self.lighter.start_websocket([self.lighter_market_id])
        await self.lighter.wait_for_orderbook(timeout=30)

        # 4. 创建子模块
        self.ob_manager = OrderBookManager(
            self.o1, self.lighter,
            self.o1_market_id, self.lighter_market_id,
        )
        self.order_mgr = OrderManager(
            self.o1, self.lighter,
            self.positions, self.data_logger,
            self.o1_market_id, self.lighter_market_id,
            self.order_quantity, self.fill_timeout,
            self.o1_tick_size,
        )

        logger.info("策略初始化完成!")
        logger.info(
            f"参数: ticker={self.ticker}, qty={self.order_quantity}, "
            f"max_pos={self.positions.max_position}, "
            f"long_thresh={self.spread.long_threshold}, "
            f"short_thresh={self.spread.short_threshold}, "
            f"fill_timeout={self.fill_timeout}s, "
            f"warmup={self.spread.warmup_samples}"
        )

    async def run(self):
        """主循环"""
        logger.info("启动主循环...")
        logger.info(f"预热阶段: 需采集 {self.spread.warmup_samples} 个价差样本")

        loop_count = 0

        while not self._stop_flag:
            try:
                loop_count += 1
                await self._main_loop_iteration(loop_count)
                await asyncio.sleep(1)  # 1 秒/周期
            except asyncio.CancelledError:
                logger.info("主循环被取消")
                break
            except Exception as e:
                logger.error(f"主循环异常: {e}", exc_info=True)
                await asyncio.sleep(2)

    async def _main_loop_iteration(self, loop_count: int):
        """主循环单次迭代"""
        # 1. 刷新两端订单簿
        ready = await self.ob_manager.refresh_all()
        if not ready:
            if loop_count % 10 == 0:
                logger.warning("订单簿数据不完整, 跳过本轮")
            return

        o1_bid = self.ob_manager.get_o1_bid()
        o1_ask = self.ob_manager.get_o1_ask()
        lighter_bid = self.ob_manager.get_lighter_bid()
        lighter_ask = self.ob_manager.get_lighter_ask()

        # 2. 更新价差分析器
        self.spread.update(lighter_bid, lighter_ask, o1_bid, o1_ask)

        # 3. 记录价差
        stats = self.spread.get_stats()
        signal, spread_value = self.spread.check_signal()

        self.data_logger.log_spread(
            o1_bid=o1_bid, o1_ask=o1_ask,
            lighter_bid=lighter_bid, lighter_ask=lighter_ask,
            diff_long=stats["diff_long"],
            diff_short=stats["diff_short"],
            avg_long=stats["avg_long"],
            avg_short=stats["avg_short"],
            signal=signal,
        )

        # 4. 定期日志
        if loop_count % 30 == 0:
            self._log_status(stats)

        # 5. 预热检查
        if not self.spread.is_warmed_up:
            if loop_count % 10 == 0:
                logger.info(
                    f"预热中: {self.spread.sample_count}"
                    f"/{self.spread.warmup_samples} 样本"
                )
            return

        # 6. 余额检查
        if time.time() - self._last_balance_check > BALANCE_CHECK_INTERVAL:
            await self._check_balances()

        # 7. 风险检查
        if not self.positions.check_risk():
            logger.error("风险超限, 暂停交易")
            return

        # 8. 检测套利信号
        if signal and not self.order_mgr.is_busy:
            await self._handle_signal(signal, o1_bid, o1_ask, lighter_bid, lighter_ask)

    async def _handle_signal(
        self,
        signal: str,
        o1_bid: Decimal,
        o1_ask: Decimal,
        lighter_bid: Decimal,
        lighter_ask: Decimal,
    ):
        """处理套利信号"""
        if signal == "long_01":
            if not self.positions.can_long_o1():
                logger.info("01 多头仓位已满, 跳过 long_01")
                return
            logger.info(
                f"触发 long_01: lighter_bid={lighter_bid} > o1_ask={o1_ask}"
            )
            await self.order_mgr.execute_long_o1(o1_ask, lighter_bid)

        elif signal == "short_01":
            if not self.positions.can_short_o1():
                logger.info("01 空头仓位已满, 跳过 short_01")
                return
            logger.info(
                f"触发 short_01: o1_bid={o1_bid} > lighter_ask={lighter_ask}"
            )
            await self.order_mgr.execute_short_o1(o1_bid, lighter_ask)

    async def _check_balances(self):
        """定期检查两端余额"""
        self._last_balance_check = time.time()

        try:
            o1_balance = await self.o1.get_balance()
            lighter_balance = await self.lighter.get_balance()

            logger.debug(
                f"余额: 01={o1_balance} USDC, Lighter={lighter_balance} USDC"
            )

            if o1_balance < MIN_BALANCE or lighter_balance < MIN_BALANCE:
                logger.error(
                    f"余额不足! 01={o1_balance}, Lighter={lighter_balance} "
                    f"(最低={MIN_BALANCE})"
                )
                self._stop_flag = True
        except Exception as e:
            logger.warning(f"余额检查失败: {e}")

    def _log_status(self, spread_stats: dict):
        """定期日志输出"""
        pos_stats = self.positions.get_stats()
        staleness = self.ob_manager.get_staleness()

        logger.info(
            f"状态 | "
            f"采样={spread_stats['sample_count']} | "
            f"diff_L={spread_stats['diff_long']:.2f} "
            f"diff_S={spread_stats['diff_short']:.2f} | "
            f"avg_L={spread_stats['avg_long']:.2f} "
            f"avg_S={spread_stats['avg_short']:.2f} | "
            f"01pos={pos_stats['o1_position']} "
            f"Lpos={pos_stats['lighter_position']} "
            f"net={pos_stats['net_position']} | "
            f"trades: L={pos_stats['total_long']} S={pos_stats['total_short']} | "
            f"age: 01={staleness['o1_age']:.1f}s L={staleness['lighter_age']:.1f}s"
        )

    async def shutdown(self):
        """
        优雅退出:
          1. 停止主循环
          2. 取消所有挂单
          3. 平仓两端
          4. 断开连接
        """
        logger.info("=" * 60)
        logger.info("开始优雅退出...")
        logger.info("=" * 60)

        self._stop_flag = True

        # 1. 取消所有01挂单
        try:
            logger.info("取消 01exchange 所有挂单...")
            await self.o1.cancel_all_orders(self.o1_market_id)
        except Exception as e:
            logger.warning(f"取消01挂单失败: {e}")

        # 2. 取消所有 Lighter 挂单
        try:
            logger.info("取消 Lighter 所有挂单...")
            await self.lighter.cancel_all_orders(self.lighter_market_id)
        except Exception as e:
            logger.warning(f"取消 Lighter 挂单失败: {e}")

        # 3. 平仓两端
        await self._close_all_positions()

        # 4. 断开连接
        try:
            await self.lighter.disconnect()
        except Exception as e:
            logger.warning(f"断开 Lighter 失败: {e}")

        try:
            await self.o1.disconnect()
        except Exception as e:
            logger.warning(f"断开 01exchange 失败: {e}")

        # 5. 关闭日志
        self.data_logger.close()

        logger.info("退出完成!")

    async def _close_all_positions(self):
        """平仓两端所有仓位"""
        max_retries = 5

        for attempt in range(max_retries):
            o1_pos = self.positions.o1_position
            lighter_pos = self.positions.lighter_position

            if abs(o1_pos) < self.order_quantity / 10 and abs(lighter_pos) < self.order_quantity / 10:
                logger.info("两端仓位已清空")
                return

            logger.info(
                f"平仓中 (尝试 {attempt + 1}/{max_retries}): "
                f"01={o1_pos}, Lighter={lighter_pos}"
            )

            # 平仓01端
            if abs(o1_pos) >= self.order_quantity / 10:
                try:
                    await self.o1.close_position(self.o1_market_id, o1_pos)
                    self.positions.o1_position = Decimal("0")
                except Exception as e:
                    logger.warning(f"01平仓失败: {e}")

            # 平仓 Lighter 端
            if abs(lighter_pos) >= self.order_quantity / 10:
                try:
                    await self.lighter.close_position(
                        self.lighter_market_id, lighter_pos
                    )
                    self.positions.lighter_position = Decimal("0")
                except Exception as e:
                    logger.warning(f"Lighter平仓失败: {e}")

            await asyncio.sleep(2)

        logger.warning("平仓未完全成功, 请手动检查仓位!")

    def request_stop(self):
        """请求停止 (由信号处理器调用)"""
        logger.info("收到停止请求")
        self._stop_flag = True
