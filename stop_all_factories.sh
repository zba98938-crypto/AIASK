#!/bin/bash
#
# 停止所有策略工厂进程
# 文件: stop_all_factories.sh
#
# 功能: 优雅地停止所有策略工厂相关进程
# 使用: bash stop_all_factories.sh
#

set -e

# ============================================
# 配置
# ============================================

# 项目路径：默认定位到本脚本所在的 copied AIASK integration root；可用 AIASK_PROJECT_ROOT 覆盖。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${AIASK_PROJECT_ROOT:-$SCRIPT_DIR}"
cd "$PROJECT_DIR" || exit 1
export AIASK_PROJECT_ROOT="$PROJECT_DIR"
if [ -z "${AIASK_RUNTIME_HOME:-}" ]; then
    if [ -n "${LOCALAPPDATA:-}" ]; then
        AIASK_RUNTIME_HOME="$LOCALAPPDATA/hermes-aiask"
    else
        AIASK_RUNTIME_HOME="$HOME/.aiask"
    fi
fi
export AIASK_RUNTIME_HOME
export AIASK_SQLITE_PATH="${AIASK_SQLITE_PATH:-$AIASK_RUNTIME_HOME/data/db/akshare_mcp.sqlite3}"
export AKSHARE_MCP_SQLITE_PATH="${AKSHARE_MCP_SQLITE_PATH:-$AIASK_SQLITE_PATH}"
mkdir -p "$AIASK_RUNTIME_HOME/data/db"

LOG_DIR="$AIASK_RUNTIME_HOME/logs"
FACTORY_PID_FILE="${LOG_DIR}/factory_24h.pid"
MONITOR_PID_FILE="${LOG_DIR}/monitor_24h.pid"

# ============================================
# 颜色定义
# ============================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ============================================
# 辅助函数
# ============================================

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_section() {
    echo ""
    echo "=========================================="
    echo "$1"
    echo "=========================================="
}

# 停止单个进程
stop_process() {
    local pid=$1
    local name=$2

    if ! kill -0 $pid 2>/dev/null; then
        log_warn "$name (PID $pid) 已经停止"
        return 0
    fi

    log_info "停止 $name (PID $pid)..."

    # 优雅停止
    kill -TERM $pid 2>/dev/null || true

    # 等待最多10秒
    local count=0
    while kill -0 $pid 2>/dev/null && [ $count -lt 10 ]; do
        sleep 1
        count=$((count + 1))
    done

    # 检查是否还在运行
    if kill -0 $pid 2>/dev/null; then
        log_warn "优雅停止超时，强制终止..."
        kill -KILL $pid 2>/dev/null || true
        sleep 1
    fi

    # 最终验证
    if kill -0 $pid 2>/dev/null; then
        log_error "无法停止进程 $pid"
        return 1
    else
        log_info "✅ $name 已停止"
        return 0
    fi
}

# 通过PID文件停止
stop_from_pid_file() {
    local pid_file=$1
    local name=$2

    if [ ! -f "$pid_file" ]; then
        log_warn "$name PID文件不存在: $pid_file"
        return 0
    fi

    local pid=$(cat "$pid_file")

    if [ -z "$pid" ]; then
        log_warn "$name PID文件为空"
        rm -f "$pid_file"
        return 0
    fi

    stop_process $pid "$name"
    rm -f "$pid_file"
}

# 查找并停止所有匹配的进程
stop_by_pattern() {
    local pattern=$1
    local name=$2

    log_info "查找 $name 进程..."

    local pids=$(ps aux | grep "$pattern" | grep -v grep | awk '{print $2}')

    if [ -z "$pids" ]; then
        log_info "未找到 $name 进程"
        return 0
    fi

    local count=0
    for pid in $pids; do
        stop_process $pid "$name"
        count=$((count + 1))
    done

    log_info "停止了 $count 个 $name 进程"
}

# ============================================
# 主流程
# ============================================

main() {
    log_section "停止所有策略工厂进程"

    # 1. 从PID文件停止
    log_info "尝试从PID文件停止..."
    stop_from_pid_file "$FACTORY_PID_FILE" "策略工厂"
    stop_from_pid_file "$MONITOR_PID_FILE" "监控脚本"

    sleep 1

    # 2. 按模式查找并停止
    log_info "查找所有相关进程..."
    stop_by_pattern "run_strategy_factory_quality_session" "策略工厂会话"
    stop_by_pattern "run_all_factories" "全工厂启动器"
    stop_by_pattern "run_three_factories" "三工厂启动器"
    stop_by_pattern "run_strategy_factory[^_]" "单策略工厂"
    stop_by_pattern "run_factor_mining_factory" "因子挖掘工厂"
    stop_by_pattern "run_incubation_factory" "孵化工厂"
    stop_by_pattern "run_signal_tracker" "信号追踪器"
    stop_by_pattern "monitor_strategy_factory" "监控脚本"

    sleep 1

    # 3. 最终验证
    log_section "验证清理结果"

    local remaining=$(ps aux | grep -E "run_strategy_factory|run_factor_mining|run_incubation|run_signal_tracker|monitor_strategy_factory" | grep -v grep | wc -l)

    if [ "$remaining" -eq 0 ]; then
        log_info "✅ 所有策略工厂进程已成功停止"
    else
        log_warn "⚠️  发现 $remaining 个残留进程"
        echo ""
        echo "残留进程:"
        ps aux | grep -E "run_strategy_factory|run_factor_mining|run_incubation|run_signal_tracker|monitor_strategy_factory" | grep -v grep
        echo ""
        log_warn "可能需要手动停止这些进程"
    fi

    # 4. 清理PID文件
    log_info "清理PID文件..."
    rm -f "$LOG_DIR"/*.pid 2>/dev/null || true
    log_info "✅ PID文件已清理"

    log_section "停止完成"

    echo ""
    echo "📊 当前状态"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "策略工厂: 已停止"
    echo "监控脚本: 已停止"
    echo "系统状态: 清洁"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    echo "重新启动:"
    echo "  bash start_four_factories_24h.sh"
    echo ""
}

# 执行主流程
main

exit 0
