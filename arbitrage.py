#!/usr/bin/env python3
"""
01exchange ↔ Lighter 跨交易所套利主程序

Usage:
    python arbitrage.py --size 0.001 --max-position 0.01
    python arbitrage.py --ticker BTC --size 0.001 --max-position 0.01 --long-threshold 10 --short-threshold 10
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from decimal import Decimal

from dotenv import load_dotenv

from exchanges.o1_client import O1ExchangeClient
from exchanges.lighter_client import LighterClient
from strategy.arb_strategy import ArbStrategy
from helpers.logger import setup_logger
from helpers.telegram import TelegramNotifier


def parse_args():
    parser = argparse.ArgumentParser(
        description="01exchange ↔ Lighter 跨交易所套利"
    )
    parser.add_argument(
        "--ticker", default="BTC",
        help="交易标的 (默认: BTC)",
    )
    parser.add_argument(
        "--size", required=True, type=Decimal,
        help="每笔订单数量",
    )
    parser.add_argument(
        "--max-position", required=True, type=Decimal,
        help="最大持仓 (单边)",
    )
    parser.add_argument(
        "--min-spread", default=Decimal("0"), type=Decimal,
        help="最小价差绝对值, 低于此值不交易 (默认: 0, 不限制)",
    )
    parser.add_argument(
        "--long-threshold", default=Decimal("10"), type=Decimal,
        help="做多阈值偏移 (默认: 10)",
    )
    parser.add_argument(
        "--short-threshold", default=Decimal("10"), type=Decimal,
        help="做空阈值偏移 (默认: 10)",
    )
    parser.add_argument(
        "--fill-timeout", default=5, type=int,
        help="Maker 单超时秒数 (默认: 5, 期间非破坏性查询成交状态)",
    )
    parser.add_argument(
        "--warmup-samples", default=100, type=int,
        help="预热样本数 (默认: 100)",
    )
    parser.add_argument(
        "--tick-size", default=Decimal("10"), type=Decimal,
        help="01exchange tick size (默认: 10)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别 (默认: INFO)",
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    # 初始化日志
    logger = setup_logger(level=args.log_level)

    # 加载环境变量
    load_dotenv()

    # 验证必要的环境变量
    solana_key = os.getenv("SOLANA_PRIVATE_KEY")
    if not solana_key:
        logger.error("缺少环境变量: SOLANA_PRIVATE_KEY")
        sys.exit(1)

    lighter_api_key = os.getenv("API_KEY_PRIVATE_KEY")
    if not lighter_api_key:
        logger.error("缺少环境变量: API_KEY_PRIVATE_KEY")
        sys.exit(1)

    lighter_account_index = os.getenv("LIGHTER_ACCOUNT_INDEX")
    if not lighter_account_index:
        logger.error("缺少环境变量: LIGHTER_ACCOUNT_INDEX")
        sys.exit(1)

    lighter_api_key_index = int(os.getenv("LIGHTER_API_KEY_INDEX", "3"))
    api_url = os.getenv("API_URL", "https://zo-mainnet.n1.xyz")

    # Telegram 通知 (可选)
    tg = TelegramNotifier(
        bot_token=os.getenv("TG_BOT_TOKEN", ""),
        chat_id=os.getenv("TG_CHAT_ID", ""),
    )

    # 创建交易所客户端
    o1_client = O1ExchangeClient(
        private_key=solana_key,
        api_url=api_url,
    )

    lighter_client = LighterClient(
        api_private_key=lighter_api_key,
        account_index=int(lighter_account_index),
        api_key_index=lighter_api_key_index,
    )

    # 创建策略
    strategy = ArbStrategy(
        o1_client=o1_client,
        lighter_client=lighter_client,
        ticker=args.ticker,
        order_quantity=args.size,
        max_position=args.max_position,
        min_spread=args.min_spread,
        long_threshold=args.long_threshold,
        short_threshold=args.short_threshold,
        fill_timeout=args.fill_timeout,
        warmup_samples=args.warmup_samples,
        o1_tick_size=args.tick_size,
        telegram=tg,
    )

    # 注册信号处理 (优雅退出)
    def signal_handler(sig, frame):
        logger.info(f"收到信号 {sig}, 准备退出...")
        strategy.request_stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 运行
    try:
        await strategy.initialize()
        await strategy.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt 收到")
    except Exception as e:
        logger.error(f"致命错误: {e}", exc_info=True)
    finally:
        # 屏蔽后续 Ctrl+C, 确保 shutdown 不被打断
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        logger.info("正在执行 shutdown, 请勿重复按 Ctrl+C...")
        try:
            await asyncio.wait_for(strategy.shutdown(), timeout=60)
        except asyncio.TimeoutError:
            logger.error("shutdown 超时 (60s), 请手动检查仓位!")
        except Exception as e:
            logger.error(f"shutdown 异常: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
