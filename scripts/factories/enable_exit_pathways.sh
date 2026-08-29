#!/usr/bin/env bash
# 启用 exit 路径（stale close policy）
#
# 修复问题：99% 持仓是 open，缺少 closed round-trip
# 根因：stale_paper_position_closure_enabled 默认为 False

set -euo pipefail

echo "=========================================="
echo "启用策略工厂 Exit 路径"
echo "=========================================="

# 1. 启用 stale paper position closure
export INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED=1
export INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT=100
export INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_GRACE_DAYS=0

echo ""
echo "[1] 已启用 stale paper position closure"
echo "    - ENABLED=1"
echo "    - BATCH_LIMIT=100（每轮最多平仓 100 个策略）"
echo "    - GRACE_DAYS=0（超过 max_holding_days 立即平仓）"

# 2. 验证配置
echo ""
echo "[2] 验证配置..."
python3 -c "
import os
print(f'  stale_paper_position_closure_enabled: {os.getenv(\"INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED\")}')
print(f'  batch_limit: {os.getenv(\"INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT\")}')
print(f'  grace_days: {os.getenv(\"INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_GRACE_DAYS\")}')
"

echo ""
echo "=========================================="
echo "配置完成"
echo "=========================================="
echo ""
echo "下一步："
echo "  1. 重启四工厂 supervisor（使配置生效）"
echo "  2. 等待 1-2 个运行周期"
echo "  3. 运行诊断脚本验证 closed position 增长"
echo ""
echo "验证命令："
echo "  uv run python scripts/factories/diagnose_factory_health.py"
echo ""
