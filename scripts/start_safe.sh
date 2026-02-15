#!/bin/bash
# 保守模式套利启动脚本 (高阈值 + 小仓位)

cd "$(dirname "$0")/.."
mkdir -p logs

LOGFILE="logs/arb_SAFE_$(date +%F_%H%M%S).log"

echo "=== 01-Lighter SAFE Mode ==="
echo "日志文件: $LOGFILE"
echo "启动时间: $(date)"
echo "============================"

python arbitrage.py --ticker BTC --size 0.0005 --max-position 0.005 \
    --long-threshold 20 --short-threshold 20 \
    --warmup-samples 200 --fill-timeout 3 \
    2>&1 | tee -a "$LOGFILE"
