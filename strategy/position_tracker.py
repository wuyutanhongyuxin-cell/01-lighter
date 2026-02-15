"""
跨交易所仓位跟踪器

实时跟踪两端仓位，监控净头寸风险。
"""

import logging
from decimal import Decimal
from typing import Dict

logger = logging.getLogger("arbitrage.position")


class PositionTracker:
    """双端仓位跟踪"""

    def __init__(self, max_position: Decimal, order_quantity: Decimal):
        self.max_position = max_position
        self.order_quantity = order_quantity

        # 两端仓位
        self.o1_position: Decimal = Decimal("0")
        self.lighter_position: Decimal = Decimal("0")

        # 统计
        self.total_long_trades: int = 0
        self.total_short_trades: int = 0

    @property
    def net_position(self) -> Decimal:
        """净仓位 (理想情况下应接近 0)"""
        return self.o1_position + self.lighter_position

    @property
    def net_exposure(self) -> Decimal:
        """净敞口 (绝对值)"""
        return abs(self.net_position)

    def can_long_o1(self) -> bool:
        """是否允许做多01 (01买入)"""
        return self.o1_position < self.max_position

    def can_short_o1(self) -> bool:
        """是否允许做空01 (01卖出)"""
        return self.o1_position > -self.max_position

    def update_o1(self, side: str, quantity: Decimal):
        """更新01端仓位"""
        if side == "buy":
            self.o1_position += quantity
        else:
            self.o1_position -= quantity
        logger.info(f"01 仓位更新: {side} {quantity} → 当前: {self.o1_position}")

    def update_lighter(self, side: str, quantity: Decimal):
        """更新 Lighter 端仓位"""
        if side == "buy":
            self.lighter_position += quantity
        else:
            self.lighter_position -= quantity
        logger.info(
            f"Lighter 仓位更新: {side} {quantity} → 当前: {self.lighter_position}"
        )

    def record_arb_trade(self, direction: str, quantity: Decimal):
        """
        记录一次完整的套利交易 (两端同时更新)

        direction: 'long_01' (01买 + Lighter卖) 或 'short_01' (01卖 + Lighter买)
        """
        if direction == "long_01":
            self.o1_position += quantity
            self.lighter_position -= quantity
            self.total_long_trades += 1
        elif direction == "short_01":
            self.o1_position -= quantity
            self.lighter_position += quantity
            self.total_short_trades += 1

        logger.info(
            f"套利交易: {direction} {quantity} | "
            f"01={self.o1_position} Lighter={self.lighter_position} "
            f"净={self.net_position}"
        )

    def check_risk(self) -> bool:
        """
        风险检查

        Returns:
            True = 正常, False = 风险超限
        """
        # 净仓位差异不应超过 2 倍单笔数量
        if self.net_exposure > self.order_quantity * 2:
            logger.error(
                f"仓位差异过大! 净敞口={self.net_exposure} "
                f"(阈值={self.order_quantity * 2})"
            )
            return False

        return True

    def get_stats(self) -> Dict:
        return {
            "o1_position": float(self.o1_position),
            "lighter_position": float(self.lighter_position),
            "net_position": float(self.net_position),
            "net_exposure": float(self.net_exposure),
            "max_position": float(self.max_position),
            "total_long": self.total_long_trades,
            "total_short": self.total_short_trades,
        }
