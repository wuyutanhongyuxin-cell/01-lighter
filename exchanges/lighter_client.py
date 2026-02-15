"""
Lighter 客户端 (Taker 端)

使用官方 lighter-sdk (elliottech/lighter-python)。
核心职责:
  - WebSocket 实时获取订单簿
  - Taker 市价/限价单对冲
  - 仓位与余额查询
"""

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import lighter

from exchanges.base import BaseExchangeClient

logger = logging.getLogger("arbitrage.lighter")

# Lighter mainnet
LIGHTER_MAINNET_URL = "https://mainnet.zklighter.elliot.ai"


class LighterClient(BaseExchangeClient):
    """Lighter DEX 客户端"""

    def __init__(
        self,
        api_private_key: str,
        account_index: int,
        api_key_index: int = 3,
        url: str = LIGHTER_MAINNET_URL,
    ):
        super().__init__("Lighter")
        self.url = url
        self.api_private_key = api_private_key
        self.account_index = account_index
        self.api_key_index = api_key_index

        # SDK 客户端
        self.signer_client: Optional[lighter.SignerClient] = None
        self.api_client: Optional[lighter.ApiClient] = None
        self.ws_client: Optional[lighter.WsClient] = None

        # 市场信息缓存
        self._markets: Dict[str, Dict] = {}  # symbol -> market_info
        self._market_index_map: Dict[str, int] = {}  # ticker -> market_index
        self._price_multiplier: Dict[int, int] = {}  # market_index -> multiplier
        self._size_multiplier: Dict[int, int] = {}
        self._price_decimals: Dict[int, int] = {}
        self._size_decimals: Dict[int, int] = {}

        # 实时订单簿 (由 WsClient 回调更新)
        self._orderbooks: Dict[int, Dict] = {}  # market_index -> orderbook
        self._orderbook_ready = asyncio.Event()

        # 账户状态 (由 WsClient 回调更新)
        self._account_state: Dict = {}
        self._account_ready = asyncio.Event()

        # 订单索引计数器 (client_order_index 必须全局唯一)
        self._order_counter = int(time.time() * 1000) % 1_000_000

        # WebSocket 任务
        self._ws_task: Optional[asyncio.Task] = None

    # ========== 连接管理 ==========

    async def connect(self):
        """初始化: 创建 SDK 客户端 + 加载市场信息 + 启动 WebSocket"""
        logger.info("Lighter 连接中...")

        # 1. API 客户端 (查询用)
        self.api_client = lighter.ApiClient(
            configuration=lighter.Configuration(host=self.url)
        )

        # 2. 签名客户端 (交易用)
        self.signer_client = lighter.SignerClient(
            url=self.url,
            api_private_keys={self.api_key_index: self.api_private_key},
            account_index=self.account_index,
        )

        # 3. 加载市场信息
        await self._load_markets()

        self._connected = True
        logger.info(f"Lighter 连接成功, 账户索引: {self.account_index}")

    async def disconnect(self):
        """断开连接"""
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self.ws_client and self.ws_client.ws:
            try:
                await self.ws_client.ws.close()
            except Exception:
                pass

        if self.signer_client:
            await self.signer_client.close()
        if self.api_client:
            await self.api_client.close()

        self._connected = False
        logger.info("Lighter 已断开")

    # ========== 市场信息 ==========

    async def _load_markets(self):
        """加载市场信息 (精度、market_index 等)"""
        order_api = lighter.OrderApi(self.api_client)
        order_books_resp = await order_api.order_books()

        for market in order_books_resp.order_books:
            symbol = market.symbol
            market_index = int(market.market_id)
            price_dec = int(market.supported_price_decimals)
            size_dec = int(market.supported_size_decimals)

            self._markets[symbol] = {
                "symbol": symbol,
                "market_index": market_index,
                "price_decimals": price_dec,
                "size_decimals": size_dec,
            }
            self._market_index_map[symbol] = market_index
            self._price_multiplier[market_index] = pow(10, price_dec)
            self._size_multiplier[market_index] = pow(10, size_dec)
            self._price_decimals[market_index] = price_dec
            self._size_decimals[market_index] = size_dec

            logger.debug(
                f"Lighter 市场 {symbol}: index={market_index}, "
                f"price_dec={price_dec}, size_dec={size_dec}"
            )

        logger.info(f"Lighter 已加载 {len(self._markets)} 个市场")

    def get_market_index(self, ticker: str) -> int:
        """根据 ticker 获取 market_index"""
        for key in [ticker, f"{ticker}_PERP", f"{ticker}-PERP", f"{ticker}/USD"]:
            if key in self._market_index_map:
                return self._market_index_map[key]
        raise ValueError(
            f"Lighter 未找到市场 '{ticker}', "
            f"可用: {list(self._market_index_map.keys())}"
        )

    # ========== WebSocket 订单簿 ==========

    def _on_order_book_update(self, market_id, order_book):
        """WsClient 订单簿更新回调"""
        mid = int(market_id) if isinstance(market_id, str) else market_id
        self._orderbooks[mid] = order_book
        if not self._orderbook_ready.is_set():
            self._orderbook_ready.set()
            logger.info(f"Lighter 订单簿就绪 (market={mid})")

    def _on_account_update(self, account_id, account):
        """WsClient 账户更新回调"""
        self._account_state = account
        if not self._account_ready.is_set():
            self._account_ready.set()
            logger.info(f"Lighter 账户状态就绪 (account={account_id})")

    async def start_websocket(self, market_indices: List[int]):
        """启动 WebSocket 订阅 (在后台运行)"""
        self.ws_client = lighter.WsClient(
            order_book_ids=market_indices,
            account_ids=[self.account_index],
            on_order_book_update=self._on_order_book_update,
            on_account_update=self._on_account_update,
        )

        async def _ws_loop():
            """WebSocket 主循环, 带重连"""
            while True:
                try:
                    logger.info("Lighter WebSocket 连接中...")
                    await self.ws_client.run_async()
                except asyncio.CancelledError:
                    logger.info("Lighter WebSocket 已取消")
                    break
                except Exception as e:
                    logger.warning(f"Lighter WebSocket 断开: {e}, 3秒后重连...")
                    await asyncio.sleep(3)

        self._ws_task = asyncio.create_task(_ws_loop())
        logger.info(f"Lighter WebSocket 已启动, 订阅市场: {market_indices}")

    async def wait_for_orderbook(self, timeout: float = 30):
        """等待订单簿数据就绪"""
        try:
            await asyncio.wait_for(self._orderbook_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Lighter 订单簿超时未就绪")

    # ========== 订单簿 ==========

    async def get_orderbook(self, market_id) -> Dict[str, Any]:
        """
        获取订单簿 (优先从 WebSocket 缓存获取)

        返回: {'bids': [[price, size], ...], 'asks': [[price, size], ...]}
        """
        if isinstance(market_id, str):
            market_id = self.get_market_index(market_id)

        # 优先用 WebSocket 缓存 (价格已是人类可读格式)
        if market_id in self._orderbooks:
            return self._format_orderbook(
                market_id, self._orderbooks[market_id], from_ws=True
            )

        # fallback: REST API (价格可能是原始整数)
        order_api = lighter.OrderApi(self.api_client)
        details = await order_api.order_book_details(market_id=market_id)

        raw_ob = {
            "asks": getattr(details, "asks", []),
            "bids": getattr(details, "bids", []),
        }
        return self._format_orderbook(market_id, raw_ob, from_ws=False)

    def _format_orderbook(
        self, market_index: int, raw_ob: Dict, from_ws: bool = True
    ) -> Dict[str, Any]:
        """
        将订单簿数据转换为标准格式 [[price, size], ...]

        关键: WebSocket 返回的价格是人类可读的小数字符串 (如 "3327.46"),
              不需要除以 multiplier。
              REST API 可能返回原始整数, 需要除以 multiplier。
        """
        price_mult = self._price_multiplier.get(market_index, 100)
        size_mult = self._size_multiplier.get(market_index, 10000)

        def _parse_entry(entry):
            if isinstance(entry, dict):
                p = float(entry.get("price", 0))
                s = float(entry.get("size", 0))
            elif hasattr(entry, "price"):
                p = float(entry.price)
                s = float(entry.size)
            else:
                p = float(entry[0])
                s = float(entry[1])

            # REST API 返回原始整数, 需要转换
            if not from_ws:
                p = p / price_mult
                s = s / size_mult

            return p, s

        bids = []
        for entry in raw_ob.get("bids", []):
            p, s = _parse_entry(entry)
            if s > 0:
                bids.append([p, s])

        asks = []
        for entry in raw_ob.get("asks", []):
            p, s = _parse_entry(entry)
            if s > 0:
                asks.append([p, s])

        # 排序: bids 降序, asks 升序
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        return {"bids": bids, "asks": asks}

    def get_ws_bbo(self, market_index: int) -> Optional[Dict]:
        """从 WebSocket 缓存获取 BBO (最低延迟)"""
        if market_index not in self._orderbooks:
            return None

        ob = self._format_orderbook(market_index, self._orderbooks[market_index])
        return self.get_bbo(ob)

    # ========== 下单 ==========

    def _next_client_order_index(self) -> int:
        """生成全局唯一的 client_order_index"""
        self._order_counter += 1
        return self._order_counter

    async def place_order(
        self,
        market_id,
        side: str,
        price: Decimal,
        size: Decimal,
        order_type: str = "limit",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        在 Lighter 下单

        Args:
            market_id: market_index (int) 或 ticker (str)
            side: 'buy' 或 'sell'
            price: 价格 (Decimal, 人类可读)
            size: 数量 (Decimal, 人类可读)
            order_type: 'limit' / 'market' / 'ioc'
            reduce_only: 是否仅减仓
        """
        if isinstance(market_id, str):
            market_id = self.get_market_index(market_id)

        price_mult = self._price_multiplier.get(market_id, 100)
        size_mult = self._size_multiplier.get(market_id, 10000)

        raw_price = int(float(price) * price_mult)
        raw_size = int(float(size) * size_mult)

        is_ask = side == "sell"
        client_order_index = self._next_client_order_index()

        # 映射 order_type 和 time_in_force
        if order_type == "market":
            sdk_order_type = self.signer_client.ORDER_TYPE_MARKET
            sdk_tif = self.signer_client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
            order_expiry = self.signer_client.DEFAULT_IOC_EXPIRY
        elif order_type == "ioc":
            sdk_order_type = self.signer_client.ORDER_TYPE_LIMIT
            sdk_tif = self.signer_client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
            order_expiry = self.signer_client.DEFAULT_IOC_EXPIRY
        else:
            sdk_order_type = self.signer_client.ORDER_TYPE_LIMIT
            sdk_tif = self.signer_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
            order_expiry = self.signer_client.DEFAULT_28_DAY_ORDER_EXPIRY

        logger.info(
            f"Lighter 下单: {'ASK' if is_ask else 'BID'} {size}@{price} "
            f"(raw: {raw_size}@{raw_price}) type={order_type} "
            f"reduce_only={reduce_only} coi={client_order_index}"
        )

        tx, tx_hash, error = await self.signer_client.create_order(
            market_index=market_id,
            client_order_index=client_order_index,
            base_amount=raw_size,
            price=raw_price,
            is_ask=is_ask,
            order_type=sdk_order_type,
            time_in_force=sdk_tif,
            reduce_only=reduce_only,
            order_expiry=order_expiry,
        )

        if error:
            logger.error(f"Lighter 下单失败: {error}")
            raise RuntimeError(f"Lighter 下单失败: {error}")

        logger.info(f"Lighter 下单成功: tx_hash={tx_hash}, coi={client_order_index}")

        return {
            "client_order_index": client_order_index,
            "tx_hash": tx_hash,
            "side": side,
            "price": price,
            "size": size,
            "tx": tx,
        }

    async def place_taker_order(
        self,
        market_id,
        side: str,
        size: Decimal,
        slippage: Decimal = Decimal("0.002"),
    ) -> Dict[str, Any]:
        """
        下 Taker 单 (用于对冲)

        使用限价单 + IOC 模拟市价单:
          买入: best_ask * (1 + slippage)
          卖出: best_bid * (1 - slippage)
        """
        if isinstance(market_id, str):
            market_id = self.get_market_index(market_id)

        # 获取当前 BBO
        bbo = self.get_ws_bbo(market_id)
        if bbo is None:
            ob = await self.get_orderbook(market_id)
            bbo = self.get_bbo(ob)

        if side == "buy":
            if bbo["best_ask"] is None:
                raise RuntimeError("Lighter 无卖单, 无法买入")
            taker_price = bbo["best_ask"] * (Decimal("1") + slippage)
        else:
            if bbo["best_bid"] is None:
                raise RuntimeError("Lighter 无买单, 无法卖出")
            taker_price = bbo["best_bid"] * (Decimal("1") - slippage)

        return await self.place_order(
            market_id=market_id,
            side=side,
            price=taker_price,
            size=size,
            order_type="ioc",
            reduce_only=False,
        )

    async def cancel_order(self, market_id, order_id) -> bool:
        """撤单 (使用 order_index)"""
        if isinstance(market_id, str):
            market_id = self.get_market_index(market_id)

        tx, tx_hash, error = await self.signer_client.cancel_order(
            market_index=market_id,
            order_index=int(order_id),
        )

        if error:
            logger.warning(f"Lighter 撤单失败: {error}")
            return False

        logger.info(f"Lighter 撤单成功: order_index={order_id}")
        return True

    async def cancel_all_orders(self, market_id):
        """取消该市场所有挂单"""
        if isinstance(market_id, str):
            market_id = self.get_market_index(market_id)

        tx, tx_hash, error = await self.signer_client.cancel_all_orders(
            market_index=market_id,
        )

        if error:
            logger.warning(f"Lighter 批量撤单失败: {error}")
        else:
            logger.info(f"Lighter 批量撤单成功: market={market_id}")

    # ========== 仓位与余额 ==========

    async def get_position(self, market_id) -> Decimal:
        """获取持仓"""
        if isinstance(market_id, str):
            market_id = self.get_market_index(market_id)

        # 优先从 WebSocket 账户状态获取
        if self._account_state:
            positions = self._account_state.get("positions", [])
            for pos in positions:
                pos_market = pos.get("market_index", pos.get("market_id", -1))
                if int(pos_market) == market_id:
                    size = pos.get("size", pos.get("position", "0"))
                    return Decimal(str(size))

        # fallback: REST API
        try:
            account_api = lighter.AccountApi(self.api_client)
            account = await account_api.account(
                by="index", value=str(self.account_index)
            )
            if hasattr(account, "positions"):
                for pos in account.positions:
                    if int(pos.market_index) == market_id:
                        return Decimal(str(pos.size))
        except Exception as e:
            logger.debug(f"Lighter 获取仓位失败: {e}")

        return Decimal("0")

    async def get_balance(self) -> Decimal:
        """获取 USDC 余额"""
        # 从 WebSocket 账户状态
        if self._account_state:
            balance = self._account_state.get("balance", self._account_state.get("collateral", "0"))
            if balance:
                return Decimal(str(balance))

        # fallback: REST API
        try:
            account_api = lighter.AccountApi(self.api_client)
            account = await account_api.account(
                by="index", value=str(self.account_index)
            )
            if hasattr(account, "balance"):
                return Decimal(str(account.balance))
            if hasattr(account, "collateral"):
                return Decimal(str(account.collateral))
        except Exception as e:
            logger.warning(f"Lighter 获取余额失败: {e}")

        return Decimal("0")

    # ========== 平仓 ==========

    async def close_position(self, market_id, current_position: Decimal):
        """市价平仓"""
        if current_position == 0:
            return

        if isinstance(market_id, str):
            market_id = self.get_market_index(market_id)

        if current_position > 0:
            side = "sell"
        else:
            side = "buy"

        logger.info(f"Lighter 平仓: {side} {abs(current_position)}")

        await self.place_taker_order(
            market_id=market_id,
            side=side,
            size=abs(current_position),
            slippage=Decimal("0.005"),  # 平仓时用更大的滑点
        )
