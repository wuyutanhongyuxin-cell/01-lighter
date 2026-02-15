"""
价差采样与分析器

每秒采样一次两端价差，维护滑动窗口均值。
预热阶段收集足够样本后才允许触发交易信号。
"""

import logging
import time
from collections import deque
from decimal import Decimal
from typing import Optional, Tuple

logger = logging.getLogger("arbitrage.spread")


class SpreadAnalyzer:
    """价差采样与动态阈值分析"""

    def __init__(
        self,
        warmup_samples: int = 100,
        long_threshold: Decimal = Decimal("10"),
        short_threshold: Decimal = Decimal("10"),
        window_size: int = 500,
    ):
        self.warmup_samples = warmup_samples
        self.long_threshold = long_threshold
        self.short_threshold = short_threshold

        # 价差历史 (滑动窗口)
        self._long_history: deque = deque(maxlen=window_size)
        self._short_history: deque = deque(maxlen=window_size)

        # 统计
        self._sample_count = 0
        self._warmed_up = False

        # 当前值
        self._last_diff_long: Optional[Decimal] = None
        self._last_diff_short: Optional[Decimal] = None
        self._avg_long: Optional[Decimal] = None
        self._avg_short: Optional[Decimal] = None

    @property
    def is_warmed_up(self) -> bool:
        return self._warmed_up

    @property
    def sample_count(self) -> int:
        return self._sample_count

    def update(
        self,
        lighter_bid: Decimal,
        lighter_ask: Decimal,
        o1_bid: Decimal,
        o1_ask: Decimal,
    ) -> None:
        """
        采样一次价差

        diff_long  = lighter_bid - o1_ask  (做多 01 信号: 01买, Lighter卖)
        diff_short = o1_bid - lighter_ask  (做空 01 信号: 01卖, Lighter买)
        """
        self._last_diff_long = lighter_bid - o1_ask
        self._last_diff_short = o1_bid - lighter_ask

        self._long_history.append(self._last_diff_long)
        self._short_history.append(self._last_diff_short)

        self._sample_count += 1

        if self._sample_count >= self.warmup_samples and not self._warmed_up:
            self._warmed_up = True
            logger.info(
                f"价差预热完成! 已采集 {self._sample_count} 个样本"
            )

        # 更新均值
        if self._long_history:
            self._avg_long = sum(self._long_history) / len(self._long_history)
        if self._short_history:
            self._avg_short = sum(self._short_history) / len(self._short_history)

    def check_signal(self) -> Tuple[Optional[str], Optional[Decimal]]:
        """
        检查是否触发套利信号

        Returns:
            (signal, spread):
              signal = 'long_01' / 'short_01' / None
              spread = 当前价差值
        """
        if not self._warmed_up:
            return None, None

        if (
            self._last_diff_long is not None
            and self._avg_long is not None
            and self._last_diff_long > self._avg_long + self.long_threshold
        ):
            return "long_01", self._last_diff_long

        if (
            self._last_diff_short is not None
            and self._avg_short is not None
            and self._last_diff_short > self._avg_short + self.short_threshold
        ):
            return "short_01", self._last_diff_short

        return None, None

    def get_stats(self) -> dict:
        """获取当前统计信息"""
        return {
            "sample_count": self._sample_count,
            "warmed_up": self._warmed_up,
            "diff_long": float(self._last_diff_long) if self._last_diff_long is not None else 0.0,
            "diff_short": float(self._last_diff_short) if self._last_diff_short is not None else 0.0,
            "avg_long": float(self._avg_long) if self._avg_long is not None else 0.0,
            "avg_short": float(self._avg_short) if self._avg_short is not None else 0.0,
            "long_threshold": float(self.long_threshold),
            "short_threshold": float(self.short_threshold),
        }
