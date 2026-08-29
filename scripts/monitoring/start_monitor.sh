#!/bin/bash
# 策略工厂监控启动脚本

cd "$(dirname "$0")/../.."

echo "=========================================="
echo "策略工厂四工厂监控系统"
echo "=========================================="

# 设置环境变量
export PYTHONIOENCODING=utf-8

# 检查是否已有监控进程
if pgrep -f "monitor_strategy_factory_rounds" > /dev/null; then
    echo "监控进程已在运行"
    ps aux | grep "monitor_strategy_factory" | grep -v grep
    echo ""
    echo "如需重启，请先停止现有进程："
    echo "  pkill -f monitor_strategy_factory_rounds"
    exit 1
fi

# 启动监控
echo "启动监控进程..."
nohup F:/Python311/python.exe -u scripts/monitoring/monitor_strategy_factory_rounds.py >> logs/monitor_v10.log 2>&1 &
PID=$!

sleep 2

# 验证启动
if ps -p $PID > /dev/null; then
    echo "✓ 监控进程已启动 (PID: $PID)"
    echo ""
    echo "查看实时记录："
    echo "  cat 策略工厂实时运行记录-v10-20260613.md"
    echo ""
    echo "查看监控日志："
    echo "  tail -f logs/monitor_v10.log"
else
    echo "✗ 监控进程启动失败"
    echo ""
    echo "检查日志："
    echo "  tail -50 logs/monitor_v10.log"
    exit 1
fi

echo "=========================================="
