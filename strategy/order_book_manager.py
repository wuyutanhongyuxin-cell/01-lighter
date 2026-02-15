"""
订单簿管理器

维护两端的 BBO (Best Bid/Offer) 缓存。
01 端通过 REST 轮询, Lighter 端通过 WebSocket 实时推送。
"""

import asyncio
import logging
import time
from decimal import Decimal
from typing import Dict, Optional

from exchanges.o1_client import O1ExchangeClient
from exchanges.lighter_client import LighterClient

logger = logging.getLogger("arbitrage.orderbook")


class OrderBookManager:
    """双端订单簿管理"""

    def __init__(
        self,
        o1_client: O1ExchangeClient,
        lighter_client: LighterClient,
        o1_market_id: int,
        lighter_market_id: int,
    ):
        self.o1 = o1_client
        self.lighter = lighter_client
        self.o1_market_id = o1_market_id
        self.lighter_market_id = lighter_market_id

        # 当前 BBO 缓存
        self.o1_bbo: Optional[Dict] = None
        self.lighter_bbo: Optional[Dict] = None

        # 更新时间戳
        self._o1_updated_at: float = 0
        self._lighter_updated_at: float = 0

    async def refresh_o1(self) -> Optional[Dict]:
        """刷新01端订单簿 (REST 轮询)"""
        try:
            ob = await self.o1.get_orderbook(self.o1_market_id)
            self.o1_bbo = self.o1.get_bbo(ob)
            self._o1_updated_at = time.time()
            return self.o1_bbo
        except Exception as e:
            logger.warning(f"刷新01订单簿失败: {e}")
            return self.o1_bbo

    def refresh_lighter(self) -> Optional[Dict]:
        """刷新 Lighter 端 BBO (从 WebSocket 缓存读取, 同步操作)"""
        bbo = self.lighter.get_ws_bbo(self.lighter_market_id)
        if bbo:
            self.lighter_bbo = bbo
            self._lighter_updated_at = time.time()
        return self.lighter_bbo

    async def refresh_all(self) -> bool:
        """
        刷新两端 BBO

        Returns:
            True = 两端数据都可用
        """
        # Lighter: 从 WebSocket 缓存 (零延迟)
        self.refresh_lighter()

        # 01: REST 请求
        await self.refresh_o1()

        return self.is_ready()

    def is_ready(self) -> bool:
        """两端数据是否都可用"""
        if not self.o1_bbo or not self.lighter_bbo:
            return False
        if self.o1_bbo.get("best_bid") is None or self.o1_bbo.get("best_ask") is None:
            return False
        if self.lighter_bbo.get("best_bid") is None or self.lighter_bbo.get("best_ask") is None:
            return False
        return True

    def get_o1_bid(self) -> Optional[Decimal]:
        return self.o1_bbo["best_bid"] if self.o1_bbo else None

    def get_o1_ask(self) -> Optional[Decimal]:
        return self.o1_bbo["best_ask"] if self.o1_bbo else None

    def get_lighter_bid(self) -> Optional[Decimal]:
        return self.lighter_bbo["best_bid"] if self.lighter_bbo else None

    def get_lighter_ask(self) -> Optional[Decimal]:
        return self.lighter_bbo["best_ask"] if self.lighter_bbo else None

    def get_staleness(self) -> Dict[str, float]:
        """获取数据新鲜度 (秒)"""
        now = time.time()
        return {
            "o1_age": now - self._o1_updated_at if self._o1_updated_at else float("inf"),
            "lighter_age": now - self._lighter_updated_at if self._lighter_updated_at else float("inf"),
        }
