"""
Telegram Bot 通知模块

异步非阻塞发送，失败不影响主逻辑。
未配置 TG_BOT_TOKEN / TG_CHAT_ID 时自动禁用。
"""

import logging
import aiohttp

logger = logging.getLogger("arbitrage.telegram")

TG_API = "https://api.telegram.org"


class TelegramNotifier:

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self._session: aiohttp.ClientSession | None = None

        if not self.enabled:
            logger.info("Telegram 通知未配置 (缺少 TG_BOT_TOKEN 或 TG_CHAT_ID), 已禁用")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_message(self, text: str):
        """发送消息到 Telegram，失败仅打日志"""
        if not self.enabled:
            return
        try:
            session = await self._get_session()
            url = f"{TG_API}/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"TG 发送失败 [{resp.status}]: {body[:200]}")
        except Exception as e:
            logger.warning(f"TG 发送异常: {e}")

    async def notify_start(self, ticker: str, qty, max_pos, long_thresh, short_thresh):
        """启动通知"""
        text = (
            f"🟢 *套利机器人启动*\n"
            f"标的: {ticker} | 单量: {qty}\n"
            f"最大仓位: {max_pos}\n"
            f"做多阈值: {long_thresh} | 做空阈值: {short_thresh}"
        )
        await self.send_message(text)

    async def notify_stop(self, reason: str, runtime_hours: float, total_trades: int):
        """停止通知"""
        text = (
            f"🔴 *机器人停止*\n"
            f"原因: {reason}\n"
            f"运行时长: {runtime_hours:.1f}h | 总交易: {total_trades} 笔"
        )
        await self.send_message(text)

    async def notify_trade(
        self, direction: str,
        o1_side: str, o1_price, o1_size,
        lighter_side: str, lighter_price, lighter_size,
        spread_captured,
        o1_position, lighter_position,
    ):
        """交易完成通知"""
        dir_label = "做多01" if direction == "long_01" else "做空01"
        text = (
            f"🔔 *交易执行: {dir_label}*\n"
            f"01: {o1_side.upper()}@{o1_price} x{o1_size}\n"
            f"Lighter: {lighter_side.upper()}@{lighter_price} x{lighter_size}\n"
            f"价差: {spread_captured}\n"
            f"仓位: 01={o1_position} Lighter={lighter_position}"
        )
        await self.send_message(text)

    async def notify_heartbeat(
        self, runtime_hours: float, total_trades: int,
        diff_long, diff_short, avg_long, avg_short,
        o1_position, lighter_position, net_position,
    ):
        """心跳状态推送"""
        text = (
            f"💓 *心跳* | 运行 {runtime_hours:.1f}h | 交易 {total_trades} 笔\n"
            f"📊 做多价差: {diff_long:.2f} (均值: {avg_long:.2f})\n"
            f"📊 做空价差: {diff_short:.2f} (均值: {avg_short:.2f})\n"
            f"💰 01: {o1_position} | Lighter: {lighter_position} | 净: {net_position}"
        )
        await self.send_message(text)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
