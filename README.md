# 01exchange ↔ Lighter | Cross-Exchange Perpetual Arbitrage Bot

<p align="center">
  <strong>Maker-Taker 跨交易所永续合约价差套利系统</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-blue?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/Protocol-Protobuf-green" />
  <img src="https://img.shields.io/badge/Chain-Solana-purple?logo=solana" />
  <img src="https://img.shields.io/badge/License-MIT-yellow" />
  <img src="https://img.shields.io/badge/Status-Production--Ready-brightgreen" />
</p>

---

## Overview

在 **01exchange** (Solana DEX) 上挂 Post-Only 限价单做 **Maker**，当 Maker 单成交后，立即在 **Lighter** (zkLighter DEX) 端执行 Taker 市价单完成对冲，捕获两端价差利润。

### 为什么这个组合有效？

| | 01exchange (Maker) | Lighter (Taker) |
|---|---|---|
| **费率** | Maker 低费率 | Standard 账户 **零费率** |
| **角色** | Post-Only 挂单等待成交 | IOC 吃单立即对冲 |
| **数据源** | REST 轮询订单簿 | WebSocket 实时推送 (50ms) |
| **优势** | Solana 链上结算 | 零费率 + 低延迟 |

---

## Architecture

```
                    ┌─────────────────────────────────┐
                    │         Main Loop (1s/cycle)     │
                    │                                  │
                    │  SpreadAnalyzer → 价差采样        │
                    │  OrderBookManager → 双端BBO       │
                    │  PositionTracker → 仓位监控       │
                    └──────────┬──────────┬────────────┘
                               │          │
                    ┌──────────▼──┐  ┌────▼───────────┐
                    │ 01exchange  │  │    Lighter      │
                    │  (Maker)    │  │   (Taker)       │
                    │             │  │                  │
                    │ Protobuf +  │  │ SDK + WebSocket  │
                    │ Solana Sign │  │ Zero Fee         │
                    └─────────────┘  └─────────────────┘

执行流程:
  ① 价差超过动态阈值 → 触发套利信号
  ② 01 Post-Only BUY/SELL (Maker)
  ③ 等待 fill_timeout 秒 → 撤单检测成交
  ④ 01 成交确认 → Lighter IOC SELL/BUY (Taker)
  ⑤ 更新仓位 + 记录日志
```

---

## Quick Start

### 1. 环境准备

```bash
# Python 3.9+
python --version

# 安装 Protocol Buffers 编译器
# Ubuntu/Debian
sudo apt install -y protobuf-compiler

# macOS
brew install protobuf

# Windows (通过 choco)
choco install protoc
```

### 2. 克隆 & 安装

```bash
git clone https://github.com/wuyutanhongyuxin-cell/01-lighter.git
cd 01-lighter

pip install -r requirements.txt
```

### 3. 配置密钥

```bash
cp .env.example .env
```

编辑 `.env` 文件:

```env
# ===== 01exchange 配置 =====
API_URL=https://zo-mainnet.n1.xyz
SOLANA_PRIVATE_KEY=你的Solana私钥_Base58格式

# ===== Lighter 配置 =====
API_KEY_PRIVATE_KEY=你的Lighter_API密钥私钥
LIGHTER_ACCOUNT_INDEX=你的账户索引
LIGHTER_API_KEY_INDEX=3
```

> **安全提示**: `.env` 已在 `.gitignore` 中，永远不要将私钥提交到版本控制。

### 4. 配置 Telegram 通知 (可选)

在 `.env` 中添加:

```env
# Telegram 通知 (不配置则自动禁用)
TG_BOT_TOKEN=123456:ABC-DEF...    # 从 @BotFather 获取
TG_CHAT_ID=987654321               # 从 @userinfobot 获取
```

配置后会推送: 启动/停止通知、交易执行通知、每 5 分钟心跳状态。

### 5. 运行

```bash
# BTC 标准模式 (推荐首次使用)
python arbitrage.py --ticker BTC --size 0.001 --max-position 0.01 \
    --long-threshold 10 --short-threshold 10 \
    --warmup-samples 100 --fill-timeout 5 \
    --tick-size 1

# BTC 保守试水 (小仓位 + 高阈值)
python arbitrage.py --ticker BTC --size 0.0005 --max-position 0.005 \
    --long-threshold 20 --short-threshold 20 \
    --warmup-samples 200 --fill-timeout 3 \
    --tick-size 1

# ETH 标准模式
python arbitrage.py --ticker ETH --size 0.01 --max-position 0.1 \
    --long-threshold 5 --short-threshold 5 \
    --warmup-samples 50 --fill-timeout 5 \
    --tick-size 0.1

# SOL 标准模式
python arbitrage.py --ticker SOL --size 0.1 --max-position 1.0 \
    --long-threshold 0.5 --short-threshold 0.5 \
    --warmup-samples 100 --fill-timeout 5 \
    --tick-size 0.01
```

---

## Production Deployment

### 方式一: 启动脚本 + screen (推荐)

项目提供了预配置的启动脚本，避免 screen 命令行引号嵌套问题：

```bash
# 给脚本加执行权限 (首次)
chmod +x scripts/*.sh

# ===== BTC 套利 =====
screen -dmS arb-btc ./scripts/start_btc.sh

# ===== ETH 套利 =====
screen -dmS arb-eth ./scripts/start_eth.sh

# ===== 保守模式 (高阈值 + 小仓位) =====
screen -dmS arb-safe ./scripts/start_safe.sh
```

脚本自动创建 `logs/` 目录并生成带时间戳的日志文件。

### 方式二: 直接前台运行 + 日志

```bash
mkdir -p logs

# BTC 标准模式
python arbitrage.py --ticker BTC --size 0.001 --max-position 0.01 \
    --long-threshold 10 --short-threshold 10 \
    --warmup-samples 100 --fill-timeout 5 \
    2>&1 | tee -a logs/arb_BTC_$(date +%F_%H%M%S).log

# ETH
python arbitrage.py --ticker ETH --size 0.01 --max-position 0.1 \
    --long-threshold 5 --short-threshold 5 \
    --warmup-samples 50 --fill-timeout 3 \
    2>&1 | tee -a logs/arb_ETH_$(date +%F_%H%M%S).log

# 保守试水
python arbitrage.py --ticker BTC --size 0.0005 --max-position 0.005 \
    --long-threshold 20 --short-threshold 20 \
    --warmup-samples 200 --fill-timeout 3 \
    2>&1 | tee -a logs/arb_SAFE_$(date +%F_%H%M%S).log
```

### screen 管理命令

```bash
# 查看所有 screen 会话
screen -ls

# 进入指定会话 (查看实时日志)
screen -r arb-btc

# 从 screen 中脱离 (不停止程序)
# 按 Ctrl+A 然后按 D

# 停止指定会话 (发送 Ctrl+C 触发优雅退出)
screen -S arb-btc -p 0 -X stuff $'\003'

# 强制终止会话
screen -S arb-btc -X quit

# 停止所有套利会话
screen -ls | grep arb | awk '{print $1}' | xargs -I {} screen -S {} -X quit
```

### 方式三: systemd 服务 (Linux 生产环境)

```ini
# /etc/systemd/system/arb-btc.service
[Unit]
Description=01-Lighter BTC Arbitrage
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/01-lighter
ExecStart=/usr/bin/python3 arbitrage.py --ticker BTC --size 0.001 --max-position 0.01 --long-threshold 10 --short-threshold 10
Restart=on-failure
RestartSec=30
StandardOutput=append:/path/to/01-lighter/logs/arb-btc.log
StandardError=append:/path/to/01-lighter/logs/arb-btc.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable arb-btc
sudo systemctl start arb-btc
sudo journalctl -u arb-btc -f  # 查看日志
```

---

## Command-Line Arguments

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `--ticker` | str | `BTC` | | 交易标的 (BTC, ETH 等) |
| `--size` | Decimal | - | **Yes** | 每笔订单数量 (BTC 单位) |
| `--max-position` | Decimal | - | **Yes** | 最大持仓量 (单边) |
| `--long-threshold` | Decimal | `10` | | 做多阈值偏移 (price units) |
| `--short-threshold` | Decimal | `10` | | 做空阈值偏移 (price units) |
| `--fill-timeout` | int | `5` | | Maker 单成交等待超时 (秒) |
| `--warmup-samples` | int | `100` | | 预热采样数 (启动后先采集再交易) |
| `--tick-size` | Decimal | `10` | | 01exchange 最小价格单位 |
| `--log-level` | str | `INFO` | | 日志级别: DEBUG/INFO/WARNING/ERROR |

### 参数调优建议

| 场景 | size | max-position | threshold | warmup | fill-timeout | tick-size |
|------|------|-------------|-----------|--------|-------------|-----------|
| **保守试水** | 0.0005 | 0.005 | 20 | 200 | 3 | 1 |
| **标准运行** | 0.001 | 0.01 | 10 | 100 | 5 | 1 |
| **激进模式** | 0.002 | 0.02 | 5 | 50 | 8 | 1 |

---

## Project Structure

```
01-lighter/
├── arbitrage.py                 # 主程序入口 (CLI + asyncio)
├── .env.example                 # 环境变量模板
├── .gitignore                   # Git 忽略规则
├── requirements.txt             # Python 依赖
├── README.md                    # 本文档
│
├── exchanges/                   # 交易所接口层
│   ├── __init__.py
│   ├── base.py                 # BaseExchangeClient 抽象基类
│   ├── o1_client.py            # 01exchange 客户端
│   │                            ├─ Protobuf 编解码
│   │                            ├─ Varint 长度前缀签名
│   │                            ├─ Session 自动续期
│   │                            ├─ 本地订单跟踪器
│   │                            └─ REST 订单簿查询
│   └── lighter_client.py       # Lighter 客户端
│                                ├─ SDK 封装 (SignerClient)
│                                ├─ WebSocket 实时订单簿
│                                ├─ 自动重连机制
│                                └─ Taker IOC 下单
│
├── strategy/                    # 策略模块
│   ├── __init__.py
│   ├── arb_strategy.py         # 主套利策略 (核心循环)
│   ├── order_manager.py        # 订单生命周期管理
│   │                            ├─ 01 Post-Only → 等待成交
│   │                            ├─ 撤单检测成交状态
│   │                            └─ Lighter Taker 对冲
│   ├── order_book_manager.py   # 双端 BBO 缓存管理
│   ├── position_tracker.py     # 跨交易所仓位跟踪
│   ├── spread_analyzer.py      # 价差采样与动态阈值
│   └── data_logger.py          # CSV 数据记录
│
├── helpers/
│   ├── __init__.py
│   ├── logger.py               # 统一日志 (控制台+文件→logs/)
│   └── telegram.py             # Telegram Bot 通知 (心跳+交易+启停)
│
└── scripts/                     # 启动脚本
    ├── start_btc.sh            # BTC 标准模式
    ├── start_eth.sh            # ETH 模式
    └── start_safe.sh           # 保守模式
```

---

## How It Works

### 1. 价差采样与信号

```
每秒采样:
  diff_long  = lighter_bid - o1_ask   → 做多01信号
  diff_short = o1_bid - lighter_ask   → 做空01信号

预热完成后 (默认100个样本):
  触发做多: diff_long  > avg_long  + long_threshold
  触发做空: diff_short > avg_short + short_threshold
```

### 2. 套利执行流程

```
=== 做多01 (01买入 + Lighter卖出) ===

  01exchange                          Lighter
  ┌─────────────────┐                ┌─────────────────┐
  │ Post-Only BUY   │   01成交后→    │ IOC SELL         │
  │ @ ask - tick    │   立即对冲→    │ @ bid * 0.998    │
  │ (Maker, 低费率)  │                │ (Taker, 零费率)  │
  └─────────────────┘                └─────────────────┘
        │                                  │
        ▼                                  ▼
  等待 fill_timeout                   即时成交
  尝试撤单检测成交                    WebSocket 确认
```

### 3. 成交检测 (01exchange 特殊机制)

01exchange 没有 WebSocket 或订单查询 API，使用**撤单探测法**:

```
下单后等待 N 秒 → 尝试撤单:
  ✓ 撤单成功     → 订单未成交 (放弃本轮)
  ✗ ORDER_NOT_FOUND → 订单已被成交! (触发对冲)
```

### 4. 风控机制

| 机制 | 说明 |
|------|------|
| **仓位限制** | 单边持仓不超过 `--max-position` |
| **净头寸监控** | 两端净敞口超过 2x 单笔量则暂停 |
| **余额监控** | 每 10s 检查，任一端 < 10 USDC → 触发平仓退出 |
| **优雅退出** | Ctrl+C → 取消挂单 → 市价平仓 → 断开连接 |
| **数据日志** | 每笔采样和交易记录到 CSV，支持事后分析 |
| **心跳监控** | 每 5 分钟推送运行状态到日志和 Telegram |
| **Telegram 通知** | 交易执行、启动/停止、心跳状态实时推送 |

---

## Output Files

运行后自动生成以下文件 (目录自动创建):

| 文件 | 说明 |
|------|------|
| `logs/arbitrage_YYYYMMDD_HHMMSS.log` | 完整运行日志 |
| `data/spreads_YYYYMMDD_HHMMSS.csv` | 每秒价差采样 (两端BBO、diff、avg、signal) |
| `data/trades_YYYYMMDD_HHMMSS.csv` | 每笔套利交易 (方向、价格、数量、捕获价差) |
| `schema.proto` / `schema_pb2.py` | 01exchange Protobuf (首次运行自动下载编译) |

---

## Key Technical Details

### 01exchange (Protobuf + Solana)

- 签名必须包含 **varint 长度前缀** (否则 Error 217)
- `CreateSession` 用 hex 编码签名 (主钱包密钥)
- `PlaceOrder/CancelOrder` 用直接字节签名 (临时 Session 密钥)
- Response 解析前必须去掉 varint 前缀
- Session 1 小时有效，提前 5 分钟自动续期
- 价格/数量为整数: `int(price * 10^decimals)`

### Lighter (SDK + WebSocket)

- 使用官方 `lighter-sdk` (elliottech/lighter-python)
- WebSocket 订单簿每 50ms 更新，价格为**人类可读小数字符串**
- 下单价格为**原始整数**: `int(price * 10^supported_price_decimals)`
- `cancel_order` 使用 `order_index` (非 `client_order_index`)
- 必须用 `create_order` 而非 `sign_create_order` (Issue #98)
- Standard 账户零费率

---

## Troubleshooting

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| Error 217 SIGNATURE_VERIFICATION | 签名未包含 varint 前缀 | 检查 `_execute_action` 方法 |
| ParseFromString 报错 | Response 未去掉 varint 前缀 | 检查 `decode_varint` 调用 |
| 01 下单被拒 | 价格未转为整数 | 检查 `raw_price` 计算 |
| Session 过期 | 未及时续期 | 检查 `ensure_session` 逻辑 |
| Lighter WS 断开 | 24h 自动断开 / 网络波动 | 已有自动重连机制 |
| 订单簿数据不完整 | WS 尚未收到快照 | 等待 `wait_for_orderbook` |
| 余额不足退出 | 任一端 < 10 USDC | 充值后重新启动 |

---

## Dependencies

```
aiohttp>=3.9.0          # 异步 HTTP (01exchange REST)
python-dotenv>=1.0.0    # 环境变量管理
protobuf>=4.25.0        # Protobuf 编解码
solders>=0.20.0         # Solana 签名
lighter-sdk>=0.1.0      # Lighter 官方 SDK
websockets>=12.0        # WebSocket 连接
tenacity>=8.2.0         # 重试机制
```

---

## Disclaimer

**本项目仅供学习和研究目的。** 加密货币交易存在极高风险，使用本程序进行实盘交易所造成的任何损失由用户自行承担。请在充分了解风险的前提下谨慎使用。

---

<p align="center">
  Built with Python + asyncio | Protobuf + Solana Signing | Lighter SDK
</p>
