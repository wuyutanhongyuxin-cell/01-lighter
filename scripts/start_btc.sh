#!/bin/bash
# BTC 套利启动脚本

cd "$(dirname "$0")/.."
mkdir -p logs

LOGFILE="logs/arb_BTC_$(date +%F_%H%M%S).log"

echo "=== 01-Lighter BTC Arbitrage ==="
echo "日志文件: $LOGFILE"
echo "启动时间: $(date)"
echo "================================"

python arbitrage.py --ticker BTC --size 0.001 --max-position 0.01 \
    --long-threshold 10 --short-threshold 10 \
    --warmup-samples 100 --fill-timeout 5 \
    2>&1 | tee -a "$LOGFILE"
