#!/bin/bash
# ETH 套利启动脚本

cd "$(dirname "$0")/.."
mkdir -p logs

LOGFILE="logs/arb_ETH_$(date +%F_%H%M%S).log"

echo "=== 01-Lighter ETH Arbitrage ==="
echo "日志文件: $LOGFILE"
echo "启动时间: $(date)"
echo "================================"

python arbitrage.py --ticker ETH --size 0.01 --max-position 0.1 \
    --long-threshold 5 --short-threshold 5 \
    --warmup-samples 50 --fill-timeout 3 \
    2>&1 | tee -a "$LOGFILE"
