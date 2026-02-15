from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional, Dict, Any


class BaseExchangeClient(ABC):
    """交易所客户端基类"""

    def __init__(self, name: str):
        self.name = name
        self._connected = False

    @abstractmethod
    async def connect(self):
        """建立连接"""
        pass

    @abstractmethod
    async def disconnect(self):
        """断开连接"""
        pass

    @abstractmethod
    async def get_orderbook(self, market_id) -> Dict[str, Any]:
        """
        获取订单簿
        返回: {'bids': [[price, size], ...], 'asks': [[price, size], ...]}
        """
        pass

    @abstractmethod
    async def place_order(
        self,
        market_id,
        side: str,
        price: Decimal,
        size: Decimal,
        order_type: str = "limit",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """下单"""
        pass

    @abstractmethod
    async def cancel_order(self, market_id, order_id) -> bool:
        """撤单, 返回是否成功"""
        pass

    @abstractmethod
    async def get_position(self, market_id) -> Decimal:
        """获取当前持仓"""
        pass

    @abstractmethod
    async def get_balance(self) -> Decimal:
        """获取可用余额 (USDC)"""
        pass

    def get_bbo(self, orderbook: Dict) -> Dict[str, Optional[Decimal]]:
        """从订单簿提取 BBO (Best Bid/Offer)"""
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        return {
            "best_bid": Decimal(str(bids[0][0])) if bids else None,
            "best_ask": Decimal(str(asks[0][0])) if asks else None,
            "best_bid_size": Decimal(str(bids[0][1])) if bids else None,
            "best_ask_size": Decimal(str(asks[0][1])) if asks else None,
        }
