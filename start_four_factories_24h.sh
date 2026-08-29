#!/bin/bash
#
# 策略工厂四工厂24小时运行脚本
# 文件: start_four_factories_24h.sh
#
# 功能: 启动策略工厂、因子挖掘、孵化工厂、信号追踪器的24小时集成会话
# 使用: bash start_four_factories_24h.sh
#

set -e  # 遇到错误立即退出

# ============================================
# 配置区
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

# Python command. Prefer an explicit override, then the package venv, then the
# akshare-mcp uv project so runtime deps such as numpy are available.
PYTHON_CMD=()
PYTHON_DISPLAY=""

# 运行参数
HOURS=24              # 运行时长（小时）
PAUSE_SEC=60          # 轮间暂停（秒）
UNIVERSE_LIMIT=300    # 股票池大小
WITH_INCUBATION=true  # 启用孵化工厂

# 会话ID（自动生成）
TIMESTAMP=$(date +%Y%m%d_%H%M)
SESSION_ID="prod_${TIMESTAMP}"

# 日志目录
LOG_DIR="$AIASK_RUNTIME_HOME/logs"
mkdir -p "$LOG_DIR"

# 日志文件
FACTORY_LOG="${LOG_DIR}/factory_24h_${SESSION_ID}.log"
MONITOR_LOG="${LOG_DIR}/monitor_24h_${SESSION_ID}.log"
FACTORY_PID_FILE="${LOG_DIR}/factory_24h.pid"
MONITOR_PID_FILE="${LOG_DIR}/monitor_24h.pid"

# ============================================
# 颜色定义
# ============================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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

# 检查进程是否存在
check_existing_processes() {
    local count
    count=$(ps aux | grep -E "run_strategy_factory_quality_session|monitor_strategy_factory" | grep -v grep | wc -l)

    if [ "$count" -gt 0 ]; then
        log_warn "发现 $count 个已存在的策略工厂进程"
        log_warn "建议先停止现有进程，使用: bash stop_all_factories.sh"

        read -p "是否继续启动？(y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            log_info "用户取消启动"
            exit 0
        fi
    fi
}

# 验证Python环境
verify_python() {
    if [ -n "${AIASK_FACTORY_PYTHON:-}" ]; then
        PYTHON_CMD=("$AIASK_FACTORY_PYTHON")
    elif [ -f "packages/akshare-mcp/.venv/Scripts/python.exe" ]; then
        PYTHON_CMD=("packages/akshare-mcp/.venv/Scripts/python.exe")
    elif command -v uv >/dev/null 2>&1; then
        PYTHON_CMD=(uv run --project packages/akshare-mcp python)
    elif [ -f "F:/Python311/python.exe" ]; then
        PYTHON_CMD=("F:/Python311/python.exe")
    else
        log_error "No Python runtime found. Set AIASK_FACTORY_PYTHON or install uv."
        exit 1
    fi
    PYTHON_DISPLAY="${PYTHON_CMD[*]}"
    log_info "Python runtime: $PYTHON_DISPLAY"
    if ! "${PYTHON_CMD[@]}" -c "import sys; print(sys.executable)" >/dev/null 2>&1; then
        log_error "Python runtime failed preflight: $PYTHON_DISPLAY"
        exit 1
    fi
}

# 验证脚本存在
verify_scripts() {
    local factory_script="scripts/factories/run_strategy_factory_quality_session.py"
    local monitor_script="scripts/monitoring/monitor_strategy_factory_rounds.py"

    if [ ! -f "$factory_script" ]; then
        log_error "策略工厂脚本不存在: $factory_script"
        exit 1
    fi

    if [ ! -f "$monitor_script" ]; then
        log_warn "监控脚本不存在: $monitor_script (监控将不启动)"
        MONITOR_SCRIPT_EXISTS=false
    else
        MONITOR_SCRIPT_EXISTS=true
    fi
}

# 启动策略工厂
start_factory() {
    log_section "启动策略工厂（四工厂集成）"

    log_info "配置参数:"
    log_info "  会话ID: $SESSION_ID"
    log_info "  运行时长: ${HOURS}小时"
    log_info "  轮间暂停: ${PAUSE_SEC}秒"
    log_info "  股票池: ${UNIVERSE_LIMIT}只"
    log_info "  孵化工厂: $([ "$WITH_INCUBATION" = true ] && echo '启用' || echo '禁用')"
    log_info "  日志文件: $FACTORY_LOG"

    # 构建命令
    local cmd=("${PYTHON_CMD[@]}" scripts/factories/run_strategy_factory_quality_session.py)
    cmd+=(--hours "$HOURS")
    cmd+=(--pause-sec "$PAUSE_SEC")
    cmd+=(--universe-limit "$UNIVERSE_LIMIT")
    cmd+=(--session-id "$SESSION_ID")
    [ "$WITH_INCUBATION" = true ] && cmd+=(--with-incubation)

    # 启动
    log_info "执行命令: ${cmd[*]}"
    nohup "${cmd[@]}" > "$FACTORY_LOG" 2>&1 &

    local pid=$!
    echo $pid > "$FACTORY_PID_FILE"

    # 等待启动
    sleep 3

    # 验证进程
    if kill -0 $pid 2>/dev/null; then
        log_info "✅ 策略工厂已启动，PID: $pid"
        return 0
    else
        log_error "❌ 策略工厂启动失败"
        log_error "查看日志: tail -100 $FACTORY_LOG"
        return 1
    fi
}

# 启动监控
start_monitor() {
    if [ "$MONITOR_SCRIPT_EXISTS" = false ]; then
        log_warn "监控脚本不存在，跳过监控启动"
        return 0
    fi

    log_section "启动监控脚本"

    log_info "日志文件: $MONITOR_LOG"

    # 设置编码
    export PYTHONIOENCODING=utf-8

    # 启动
    nohup "${PYTHON_CMD[@]}" -u scripts/monitoring/monitor_strategy_factory_rounds.py \
        >> "$MONITOR_LOG" 2>&1 &

    local pid=$!
    echo $pid > "$MONITOR_PID_FILE"

    # 等待启动
    sleep 2

    # 验证进程
    if kill -0 $pid 2>/dev/null; then
        log_info "✅ 监控脚本已启动，PID: $pid"
        return 0
    else
        log_warn "⚠️  监控脚本启动失败（不影响策略工厂运行）"
        return 0
    fi
}

# 显示状态信息
show_status() {
    log_section "启动完成"

    echo ""
    echo "📊 服务状态"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if [ -f "$FACTORY_PID_FILE" ]; then
        local factory_pid=$(cat "$FACTORY_PID_FILE")
        if kill -0 $factory_pid 2>/dev/null; then
            echo "✅ 策略工厂: 运行中 (PID: $factory_pid)"
        else
            echo "❌ 策略工厂: 已停止"
        fi
    fi

    if [ -f "$MONITOR_PID_FILE" ]; then
        local monitor_pid=$(cat "$MONITOR_PID_FILE")
        if kill -0 $monitor_pid 2>/dev/null; then
            echo "✅ 监控脚本: 运行中 (PID: $monitor_pid)"
        else
            echo "⚠️  监控脚本: 已停止"
        fi
    fi

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    echo "📁 文件位置"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "策略工厂日志: $FACTORY_LOG"
    echo "监控脚本日志: $MONITOR_LOG"
    echo "策略工厂PID:  $FACTORY_PID_FILE"
    echo "监控脚本PID:  $MONITOR_PID_FILE"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    echo "📋 常用命令"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "查看实时日志:"
    echo "  tail -f $FACTORY_LOG"
    echo ""
    echo "查看监控日志:"
    echo "  tail -f $MONITOR_LOG"
    echo ""
    echo "检查进程状态:"
    echo "  ps aux | grep run_strategy_factory"
    echo ""
    echo "停止所有服务:"
    echo "  bash stop_all_factories.sh"
    echo "  或: kill \$(cat $FACTORY_PID_FILE)"
    echo ""
    echo "查看最新轮次:"
    echo "  tail -50 $FACTORY_LOG | grep completed"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    echo "⏰ 预计运行至: $(date -d "+${HOURS} hours" +"%Y-%m-%d %H:%M:%S" 2>/dev/null || date)"
    echo ""
}

# ============================================
# 主流程
# ============================================

main() {
    log_section "策略工厂四工厂24小时启动"

    # 1. 检查现有进程
    check_existing_processes

    # 2. 验证环境
    verify_python
    verify_scripts

    # 3. 启动策略工厂
    if ! start_factory; then
        log_error "策略工厂启动失败，退出"
        exit 1
    fi

    # 4. 启动监控
    start_monitor

    # 5. 显示状态
    show_status

    log_info "启动流程完成"
    log_info "策略工厂将运行${HOURS}小时"
}

# 执行主流程
main

exit 0
