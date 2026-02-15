"""
01exchange 客户端 (Maker 端)

基于 Protobuf + Solana 签名的 DEX 通信层。
关键注意事项:
  - 签名必须包含 varint 长度前缀
  - CreateSession 用 hex 编码签名, PlaceOrder 用直接字节签名
  - Response 解析前必须去掉 varint 前缀
  - 价格/数量必须转为整数 (乘以精度因子)
  - 没有 WebSocket / 订单查询 API, 必须本地跟踪
"""

import asyncio
import binascii
import logging
import os
import subprocess
import sys
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from solders.keypair import Keypair

from exchanges.base import BaseExchangeClient

logger = logging.getLogger("arbitrage.o1")

# ========== 常量 ==========
SCHEMA_URL = "https://zo-mainnet.n1.xyz/schema.proto"
SESSION_DURATION = 3600  # 1 小时
RENEW_BEFORE = 300  # 提前 5 分钟续期


# ========== Varint 编解码 ==========

def encode_varint(value: int) -> bytes:
    """编码 varint (Protobuf 标准格式)"""
    buf = bytearray()
    while value >= 0x80:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value)
    return bytes(buf)


def decode_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
    """解码 varint, 返回 (值, 新offset)"""
    shift = 0
    result = 0
    while True:
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80):
            break
        shift += 7
    return result, offset


# ========== Protobuf Schema 动态编译 ==========

def ensure_schema():
    """下载并编译 Protobuf schema (仅首次运行)"""
    # 在项目根目录查找/生成 schema_pb2.py
    schema_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proto_file = os.path.join(schema_dir, "schema.proto")
    pb2_file = os.path.join(schema_dir, "schema_pb2.py")

    if os.path.exists(pb2_file):
        return schema_dir

    logger.info("下载 Protobuf schema...")
    import urllib.request
    urllib.request.urlretrieve(SCHEMA_URL, proto_file)

    logger.info("编译 Protobuf schema...")
    result = subprocess.run(
        ["protoc", f"--python_out={schema_dir}", f"--proto_path={schema_dir}", "schema.proto"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"protoc 编译失败: {result.stderr}")

    logger.info("Schema 编译完成")
    return schema_dir


# 确保 schema 可用, 将目录加入 sys.path
_schema_dir = ensure_schema()
if _schema_dir not in sys.path:
    sys.path.insert(0, _schema_dir)

import schema_pb2  # noqa: E402


# ========== 订单跟踪器 ==========

class O1OrderTracker:
    """01exchange 本地订单跟踪器 (因为 01 没有订单查询 API)"""

    def __init__(self):
        self.active_orders: Dict[int, Dict] = {}
        self.filled_orders: List[Dict] = []

    def add_order(self, order_id: int, side: str, price: Decimal, size: Decimal):
        self.active_orders[order_id] = {
            "order_id": order_id,
            "side": side,
            "price": price,
            "size": size,
            "status": "OPEN",
            "created_at": time.time(),
        }
        logger.debug(f"订单跟踪: 新增 #{order_id} {side} {size}@{price}")

    def mark_filled(self, order_id: int) -> Optional[Dict]:
        if order_id in self.active_orders:
            order = self.active_orders.pop(order_id)
            order["status"] = "FILLED"
            order["filled_at"] = time.time()
            self.filled_orders.append(order)
            logger.info(f"订单跟踪: #{order_id} 已成交")
            return order
        return None

    def mark_cancelled(self, order_id: int):
        order = self.active_orders.pop(order_id, None)
        if order:
            logger.debug(f"订单跟踪: #{order_id} 已撤销")

    def get_active_orders(self) -> List[Dict]:
        return list(self.active_orders.values())

    def get_active_count(self) -> int:
        return len(self.active_orders)


# ========== 01exchange 客户端 ==========

class O1ExchangeClient(BaseExchangeClient):
    """01exchange Protobuf 客户端"""

    def __init__(self, private_key: str, api_url: str = "https://zo-mainnet.n1.xyz"):
        super().__init__("01exchange")
        self.api_url = api_url.rstrip("/")
        self.keypair = Keypair.from_base58_string(private_key)
        self.pubkey = str(self.keypair.pubkey())

        # Session 状态
        self.session_id: Optional[int] = None
        self.session_keypair: Optional[Keypair] = None
        self.session_created_at: float = 0

        # 市场信息缓存
        self._markets: Dict[str, Dict] = {}
        self._market_id_map: Dict[str, int] = {}  # ticker -> market_id
        self._price_decimals: Dict[int, int] = {}
        self._size_decimals: Dict[int, int] = {}

        # 账户信息
        self._account_id: Optional[int] = None

        # HTTP session
        self._http_session: Optional[aiohttp.ClientSession] = None

        # Action nonce 计数器
        self._nonce_counter = 0

        # 订单跟踪
        self.order_tracker = O1OrderTracker()

    # ========== 连接管理 ==========

    async def connect(self):
        """初始化连接: 创建 HTTP session + 加载市场信息 + 查询账户 + 创建交易 Session"""
        self._http_session = aiohttp.ClientSession()
        logger.info(f"01exchange 连接中... 钱包: {self.pubkey[:8]}...")

        await self._load_markets()
        await self._resolve_account_id()
        await self.create_session()

        self._connected = True
        logger.info("01exchange 连接成功")

    async def disconnect(self):
        """断开连接"""
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        self._connected = False
        logger.info("01exchange 已断开")

    # ========== 市场信息 ==========

    async def _load_markets(self):
        """加载市场信息 (精度、market_id 等) — 使用 GET /info"""
        url = f"{self.api_url}/info"
        async with self._http_session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"获取市场信息失败: {resp.status}")
            data = await resp.json()

        markets = data.get("markets", data) if isinstance(data, dict) else data

        for market in markets:
            symbol = market.get("symbol", "")
            market_id = market.get("marketId", 0)
            price_dec = market.get("priceDecimals", 1)
            size_dec = market.get("sizeDecimals", 5)

            self._markets[symbol] = market
            self._market_id_map[symbol] = market_id
            self._price_decimals[market_id] = price_dec
            self._size_decimals[market_id] = size_dec

            logger.debug(
                f"市场 {symbol}: id={market_id}, "
                f"price_dec={price_dec}, size_dec={size_dec}"
            )

        logger.info(f"已加载 {len(self._markets)} 个市场")

    async def _resolve_account_id(self):
        """通过 GET /user/{pubkey} 获取 account_id"""
        url = f"{self.api_url}/user/{self.pubkey}"
        try:
            async with self._http_session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    account_ids = data.get("accountIds", [])
                    if account_ids:
                        self._account_id = account_ids[0]
                        logger.info(f"01 账户 ID: {self._account_id}")
                    else:
                        logger.warning("01 未找到关联账户, 将使用本地跟踪")
                else:
                    logger.warning(f"查询01账户失败: {resp.status}")
        except Exception as e:
            logger.warning(f"查询01账户异常: {e}")

    def get_market_id(self, ticker: str) -> int:
        """根据 ticker 获取 market_id"""
        # 尝试不同格式: BTC -> BTCUSD, BTC-PERP, etc.
        for key in [ticker, f"{ticker}USD", f"{ticker}-PERP", f"{ticker}_PERP", f"{ticker}/USD"]:
            if key in self._market_id_map:
                return self._market_id_map[key]
        raise ValueError(
            f"未找到市场 '{ticker}', 可用: {list(self._market_id_map.keys())}"
        )

    def get_price_decimals(self, market_id: int) -> int:
        return self._price_decimals.get(market_id, 1)

    def get_size_decimals(self, market_id: int) -> int:
        return self._size_decimals.get(market_id, 4)

    # ========== 签名 ==========

    def _user_sign(self, message: bytes) -> bytes:
        """User Sign: 用于 CreateSession, 先 hex 编码再签名"""
        hex_msg = binascii.hexlify(message)
        return bytes(self.keypair.sign_message(hex_msg))

    def _session_sign(self, message: bytes) -> bytes:
        """Session Sign: 用于 PlaceOrder/CancelOrder, 直接签名原始字节"""
        if self.session_keypair is None:
            raise RuntimeError("Session 未初始化, 请先调用 create_session()")
        return bytes(self.session_keypair.sign_message(message))

    # ========== Protobuf Action 执行 ==========

    async def _execute_action(
        self,
        action: "schema_pb2.Action",
        keypair: Keypair,
        sign_func,
    ) -> "schema_pb2.Receipt":
        """
        执行 Protobuf Action 的完整流程:
        1. SerializeToString
        2. 加 varint 长度前缀
        3. 签名 (包含前缀的完整 message)
        4. 拼接 message + signature
        5. 发送 POST
        6. 解析 Response (去掉 varint 前缀)
        """
        # 1. 序列化
        payload = action.SerializeToString()

        # 2. 添加 varint 长度前缀 (缺这步就是 Error 217!)
        length_prefix = encode_varint(len(payload))
        message = length_prefix + payload

        # 3. 签名完整消息
        signature = sign_func(message)

        # 4. 最终数据 = 消息 + 签名
        final_data = message + signature

        # 5. 发送
        url = f"{self.api_url}/action"
        async with self._http_session.post(
            url,
            data=final_data,
            headers={"Content-Type": "application/octet-stream"},
        ) as resp:
            response_data = await resp.read()

            if resp.status != 200:
                # 尝试解析错误
                try:
                    msg_len, pos = decode_varint(response_data, 0)
                    actual = response_data[pos : pos + msg_len]
                    receipt = schema_pb2.Receipt()
                    receipt.ParseFromString(actual)
                    error_msg = f"01 Action 失败: status={resp.status}, receipt={receipt}"
                except Exception:
                    error_msg = f"01 Action 失败: status={resp.status}, body={response_data[:200]}"
                logger.error(error_msg)
                raise RuntimeError(error_msg)

        # 6. 解析 Response — 去掉 varint 前缀
        msg_len, pos = decode_varint(response_data, 0)
        actual_data = response_data[pos : pos + msg_len]

        receipt = schema_pb2.Receipt()
        receipt.ParseFromString(actual_data)

        return receipt

    # ========== Session 管理 ==========

    async def create_session(self):
        """创建交易 Session (每次生成新的临时 Keypair)"""
        self.session_keypair = Keypair()
        expiry = int(time.time()) + SESSION_DURATION

        # 注意: CreateSession 是 Action 的嵌套消息
        action = schema_pb2.Action()
        action.current_timestamp = int(time.time())
        self._nonce_counter += 1
        action.nonce = self._nonce_counter
        action.create_session.CopyFrom(
            schema_pb2.Action.CreateSession(
                user_pubkey=bytes(self.keypair.pubkey()),
                session_pubkey=bytes(self.session_keypair.pubkey()),
                expiry_timestamp=expiry,
                signature_framing=schema_pb2.Action.HEX,
            )
        )

        logger.info("创建 01exchange Session...")
        receipt = await self._execute_action(action, self.keypair, self._user_sign)

        # 检查 Receipt 错误
        if receipt.HasField("err"):
            raise RuntimeError(f"创建 Session 失败: {receipt.err}")

        self.session_id = receipt.create_session_result.session_id
        self.session_created_at = time.time()  # 用本地时间!

        logger.info(
            f"Session 创建成功: id={self.session_id}, "
            f"过期于 {SESSION_DURATION // 60} 分钟后"
        )

    async def ensure_session(self):
        """确保 Session 有效, 过期前自动重建"""
        if not self.session_id or not self.session_keypair:
            await self.create_session()
            return

        elapsed = time.time() - self.session_created_at
        if elapsed >= (SESSION_DURATION - RENEW_BEFORE):
            logger.info("Session 即将过期, 自动续期...")
            await self.create_session()

    # ========== 订单簿 ==========

    async def get_orderbook(self, market_id) -> Dict[str, Any]:
        """
        获取订单簿 — GET /market/{market_id}/orderbook

        API 返回格式: {"asks": [[price, size], ...], "bids": [[price, size], ...]}
        价格和数量已是人类可读的 double, 不需要除以精度因子。
        """
        if isinstance(market_id, str):
            market_id = self.get_market_id(market_id)

        url = f"{self.api_url}/market/{market_id}/orderbook"
        async with self._http_session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"获取订单簿失败: {resp.status}")
            data = await resp.json()

        # API 直接返回 [price, size] double 数组, 无需转换
        bids = []
        for entry in data.get("bids", []):
            if isinstance(entry, (list, tuple)):
                bids.append([float(entry[0]), float(entry[1])])
            elif isinstance(entry, dict):
                bids.append([float(entry["price"]), float(entry["size"])])

        asks = []
        for entry in data.get("asks", []):
            if isinstance(entry, (list, tuple)):
                asks.append([float(entry[0]), float(entry[1])])
            elif isinstance(entry, dict):
                asks.append([float(entry["price"]), float(entry["size"])])

        return {"bids": bids, "asks": asks}

    # ========== 下单 ==========

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
        在 01exchange 下单

        Args:
            market_id: 市场 ID (int) 或 ticker (str)
            side: 'buy' 或 'sell'
            price: 价格 (Decimal)
            size: 数量 (Decimal)
            order_type: 'post_only' / 'immediate' / 'limit'
            reduce_only: 是否仅减仓
        """
        await self.ensure_session()

        if isinstance(market_id, str):
            market_id = self.get_market_id(market_id)

        price_dec = self.get_price_decimals(market_id)
        size_dec = self.get_size_decimals(market_id)

        # 价格/数量转整数
        raw_price = int(float(price) * (10 ** price_dec))
        raw_size = int(float(size) * (10 ** size_dec))

        # FillMode 映射 (LIMIT=0, POST_ONLY=1, IOC=2, FOK=3)
        if order_type == "post_only":
            proto_fill_mode = schema_pb2.POST_ONLY
        elif order_type == "immediate":
            proto_fill_mode = schema_pb2.IMMEDIATE_OR_CANCEL
        else:
            proto_fill_mode = schema_pb2.POST_ONLY

        # Side 映射 (ASK=0, BID=1)
        proto_side = schema_pb2.BID if side == "buy" else schema_pb2.ASK

        # PlaceOrder 是 Action 的嵌套消息
        action = schema_pb2.Action()
        action.current_timestamp = int(time.time())
        self._nonce_counter += 1
        action.nonce = self._nonce_counter
        place_order_msg = schema_pb2.Action.PlaceOrder(
            session_id=self.session_id,
            market_id=market_id,
            side=proto_side,
            price=raw_price,
            size=raw_size,
            fill_mode=proto_fill_mode,
            is_reduce_only=reduce_only,
        )
        action.place_order.CopyFrom(place_order_msg)

        logger.info(
            f"01 下单: {side} {size}@{price} (raw: {raw_size}@{raw_price}) "
            f"type={order_type} reduce_only={reduce_only}"
        )

        receipt = await self._execute_action(
            action, self.session_keypair, self._session_sign
        )

        # 检查 Receipt 是否包含错误
        if receipt.HasField("err"):
            error_msg = str(receipt.err)
            raise RuntimeError(f"01 下单被拒绝: {error_msg}")

        # PlaceOrderResult: posted 有 order_id, fills 有成交列表
        result = receipt.place_order_result
        order_id = result.posted.order_id if result.HasField("posted") else 0
        fills_count = len(result.fills)
        logger.info(f"01 下单成功: order_id={order_id}, fills={fills_count}")

        return {
            "order_id": order_id,
            "side": side,
            "price": price,
            "size": size,
            "fills": fills_count,
            "receipt": receipt,
        }

    async def cancel_order(self, market_id, order_id: int) -> bool:
        """
        撤单

        返回:
            True  = 撤单成功 (订单未成交)
            False = ORDER_NOT_FOUND (订单可能已成交)
        """
        await self.ensure_session()

        if isinstance(market_id, str):
            market_id = self.get_market_id(market_id)

        # CancelOrderById 是 Action 的嵌套消息 (不是 CancelOrder)
        action = schema_pb2.Action()
        action.current_timestamp = int(time.time())
        self._nonce_counter += 1
        action.nonce = self._nonce_counter
        action.cancel_order_by_id.CopyFrom(
            schema_pb2.Action.CancelOrderById(
                session_id=self.session_id,
                order_id=order_id,
            )
        )

        try:
            receipt = await self._execute_action(
                action, self.session_keypair, self._session_sign
            )

            # 检查 Receipt 是否包含错误
            if receipt.HasField("err"):
                error_msg = str(receipt.err)
                if "ORDER_NOT_FOUND" in error_msg or "not found" in error_msg.lower():
                    logger.info(f"01 撤单: order_id={order_id} 未找到 (可能已成交)")
                    return False
                logger.warning(f"01 撤单错误: {error_msg}")
                return False

            logger.info(f"01 撤单成功: order_id={order_id}")
            return True
        except Exception as e:
            error_str = str(e)
            if "ORDER_NOT_FOUND" in error_str or "not found" in error_str.lower():
                logger.info(f"01 撤单: order_id={order_id} 未找到 (可能已成交)")
                return False
            raise

    # ========== 仓位与余额 ==========

    async def _get_account_data(self) -> Optional[Dict]:
        """获取完整账户数据 — GET /account/{account_id}"""
        if self._account_id is None:
            return None

        try:
            url = f"{self.api_url}/account/{self._account_id}"
            async with self._http_session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.debug(f"获取01账户数据失败: {resp.status}")
        except Exception as e:
            logger.debug(f"获取01账户数据异常: {e}")

        return None

    async def get_position(self, market_id) -> Decimal:
        """获取持仓 — 从 GET /account/{account_id} 提取"""
        if isinstance(market_id, str):
            market_id = self.get_market_id(market_id)

        data = await self._get_account_data()
        if data:
            positions = data.get("positions", [])
            for pos in positions:
                pos_market = pos.get("marketId", pos.get("market_id", -1))
                if int(pos_market) == market_id:
                    # 仓位数据已是人类可读格式
                    size = pos.get("size", pos.get("netSize", 0))
                    return Decimal(str(size))

        return Decimal("0")

    async def get_balance(self) -> Decimal:
        """获取 USDC 余额 — 从 GET /account/{account_id} 提取"""
        data = await self._get_account_data()
        if data:
            # 从 balances 数组提取
            balances = data.get("balances", [])
            for bal in balances:
                if bal.get("tokenId", 0) == 0:  # USDC tokenId=0
                    return Decimal(str(bal.get("balance", bal.get("amount", 0))))

            # 或者从 margins 提取
            margins = data.get("margins", {})
            if margins:
                equity = margins.get("equity", margins.get("accountValue", 0))
                if equity:
                    return Decimal(str(equity))

        return Decimal("0")

    # ========== 高层业务方法 ==========

    async def cancel_all_orders(self, market_id):
        """取消所有活跃订单"""
        if isinstance(market_id, str):
            market_id = self.get_market_id(market_id)

        active = list(self.order_tracker.active_orders.keys())
        logger.info(f"取消所有挂单: 共 {len(active)} 笔")

        for oid in active:
            try:
                cancelled = await self.cancel_order(market_id, oid)
                if cancelled:
                    self.order_tracker.mark_cancelled(oid)
                else:
                    # ORDER_NOT_FOUND — 可能已成交
                    self.order_tracker.mark_filled(oid)
            except Exception as e:
                logger.warning(f"取消订单 #{oid} 异常: {e}")

    async def close_position(self, market_id, current_position: Decimal):
        """
        市价平仓 (使用 IMMEDIATE_OR_CANCEL + reduce_only 概念)

        01exchange 平仓用 IOC 模式
        """
        if current_position == 0:
            return

        if isinstance(market_id, str):
            market_id = self.get_market_id(market_id)

        # 获取当前价格
        ob = await self.get_orderbook(market_id)
        bbo = self.get_bbo(ob)

        if current_position > 0:
            # 多头平仓 → 卖出
            side = "sell"
            # 用一个较低的价格确保成交
            close_price = bbo["best_bid"] * Decimal("0.995") if bbo["best_bid"] else Decimal("0")
        else:
            # 空头平仓 → 买入
            side = "buy"
            close_price = bbo["best_ask"] * Decimal("1.005") if bbo["best_ask"] else Decimal("0")

        logger.info(
            f"01 平仓: {side} {abs(current_position)} @ {close_price} (IOC)"
        )

        await self.place_order(
            market_id=market_id,
            side=side,
            price=close_price,
            size=abs(current_position),
            order_type="immediate",
            reduce_only=True,
        )
