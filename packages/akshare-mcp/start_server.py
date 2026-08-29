#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AKShare MCP Server 启动脚本

建议以 UTF-8 模式启动以避免编码错误，例如：
  python -X utf8 start_server.py
或在环境变量中设置 PYTHONUTF8=1 后启动。
"""
import sys
import os
import logging
from logging.handlers import RotatingFileHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))


def _apply_security_defaults() -> None:
    """Apply secure defaults; explicit MCP config can still override these values."""
    os.environ.setdefault("MCP_HOST", "127.0.0.1")
    os.environ.setdefault("MCP_ALLOW_TOKEN_PASSTHROUGH", "false")


def _configure_file_logging() -> None:
    log_file = os.environ.get("AKSHARE_MCP_LOG_FILE") or os.environ.get("MCP_LOG_FILE")
    if not log_file:
        return
    directory = os.path.dirname(log_file)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)

# 添加src到路径（必须在 import akshare_mcp 之前）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# 委托 env_loader 统一加载 .env（不覆盖已有环境变量，确保 MCP 配置优先级更高）
from akshare_mcp.env_loader import load_mcp_env  # noqa: E402
load_mcp_env(override=False)

_apply_security_defaults()

if __name__ == "__main__":
    import logging
    import sys as _sys
    
    # MCP stdio 协议要求 stdout 只传输协议数据，启动日志走 stderr
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=_sys.stderr,
    )
    _configure_file_logging()
    _log = logging.getLogger("start_server")
    
    from akshare_mcp.server import main
    
    _log.info("=" * 60)
    _log.info("AKShare MCP Server v2")
    _log.info("=" * 60)
    _log.info("Python版本: %s", sys.version)
    _log.info("工作目录: %s", os.getcwd())
    _log.info("=" * 60)
    _log.info("启动服务器...")
    
    try:
        main()
    except KeyboardInterrupt:
        _log.info("服务器已停止")
    except Exception as e:
        _log.error("错误: %s", e)
        sys.exit(1)
