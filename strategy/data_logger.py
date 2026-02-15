"""
CSV 数据记录器

记录每次采样和每笔交易，用于事后分析。
"""

import csv
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("arbitrage.data")


class DataLogger:
    """CSV 数据记录"""

    def __init__(self, log_dir: str = "."):
        self.log_dir = log_dir
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 价差采样日志
        self.spread_file = os.path.join(log_dir, f"spreads_{timestamp}.csv")
        self._spread_writer = None
        self._spread_fh = None

        # 交易日志
        self.trades_file = os.path.join(log_dir, f"trades_{timestamp}.csv")
        self._trades_writer = None
        self._trades_fh = None

        self._init_files()

    def _init_files(self):
        """初始化 CSV 文件"""
        # 价差日志
        self._spread_fh = open(self.spread_file, "w", newline="", encoding="utf-8")
        self._spread_writer = csv.writer(self._spread_fh)
        self._spread_writer.writerow([
            "timestamp",
            "o1_bid", "o1_ask",
            "lighter_bid", "lighter_ask",
            "diff_long", "diff_short",
            "avg_long", "avg_short",
            "signal",
        ])

        # 交易日志
        self._trades_fh = open(self.trades_file, "w", newline="", encoding="utf-8")
        self._trades_writer = csv.writer(self._trades_fh)
        self._trades_writer.writerow([
            "timestamp",
            "direction",
            "o1_side", "o1_price", "o1_size",
            "lighter_side", "lighter_price", "lighter_size",
            "spread_captured",
            "o1_position", "lighter_position", "net_position",
        ])

        logger.info(f"数据日志: {self.spread_file}, {self.trades_file}")

    def log_spread(
        self,
        o1_bid, o1_ask,
        lighter_bid, lighter_ask,
        diff_long, diff_short,
        avg_long, avg_short,
        signal: Optional[str] = None,
    ):
        """记录一次价差采样"""
        if self._spread_writer:
            self._spread_writer.writerow([
                datetime.now().isoformat(),
                str(o1_bid), str(o1_ask),
                str(lighter_bid), str(lighter_ask),
                str(diff_long), str(diff_short),
                str(avg_long) if avg_long else "",
                str(avg_short) if avg_short else "",
                signal or "",
            ])
            self._spread_fh.flush()

    def log_trade(
        self,
        direction: str,
        o1_side: str, o1_price: Decimal, o1_size: Decimal,
        lighter_side: str, lighter_price: Decimal, lighter_size: Decimal,
        spread_captured: Decimal,
        o1_position: Decimal, lighter_position: Decimal,
    ):
        """记录一笔套利交易"""
        if self._trades_writer:
            net = o1_position + lighter_position
            self._trades_writer.writerow([
                datetime.now().isoformat(),
                direction,
                o1_side, str(o1_price), str(o1_size),
                lighter_side, str(lighter_price), str(lighter_size),
                str(spread_captured),
                str(o1_position), str(lighter_position), str(net),
            ])
            self._trades_fh.flush()

            logger.info(
                f"交易记录: {direction} | 价差={spread_captured} | "
                f"01={o1_side}@{o1_price} Lighter={lighter_side}@{lighter_price}"
            )

    def close(self):
        """关闭文件句柄"""
        if self._spread_fh:
            self._spread_fh.close()
        if self._trades_fh:
            self._trades_fh.close()
        logger.info("数据日志已关闭")
