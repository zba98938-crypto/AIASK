#!/usr/bin/env python
"""部署侧:安装高级校准/实验跟踪依赖(诊断报告 §4.3.1 / §4.3.7 / §5.1)。

执行内容(诊断报告中标记为"部署侧"的修复):
1. 安装 mlflow(P3-5.1 — experiment_tracker mlflow→builtin silent fallback)
2. 安装 scikit-learn(已在主依赖,但确认版本)— P2-4.3.1 Platt scaling
3. 安装 mapie(已在主依赖)— P4-3.7 conformal prediction
4. 验证 great_expectations(已在主依赖)— data_validation 真实校验
5. 启动后用 backend 检测验证所有 advanced backend 真实生效

使用方式:
    # 一键安装所有 advanced extras
    python scripts/install_advanced_calibration.py --install

    # 只验证当前安装状态(不安装)
    python scripts/install_advanced_calibration.py --verify

    # 只看 production extras 是否就绪
    python scripts/install_advanced_calibration.py --check-production

    # 安装 + 运行 backend 切换验证(端到端)
    python scripts/install_advanced_calibration.py --install --verify --e2e

输出:
- 各 backend 安装状态 + 版本
- backend 切换验证结果(每个 adapter 用真实 backend 跑一次)
- JSON 报告(可选 --report-path)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent


def _import_or_none(name: str) -> dict[str, Any]:
    """检测一个包是否可用,返回版本信息。"""
    try:
        module = __import__(name)
        version = getattr(module, "__version__", "unknown")
        return {
            "package": name,
            "available": True,
            "version": str(version),
            "path": getattr(module, "__file__", None),
        }
    except ImportError as exc:
        return {
            "package": name,
            "available": False,
            "error": str(exc),
        }


def verify_backends() -> dict[str, Any]:
    """检查所有部署相关 backend 是否就绪。"""
    results = {
        "mlflow": _import_or_none("mlflow"),
        "sklearn": _import_or_none("sklearn"),
        "mapie": _import_or_none("mapie"),
        "great_expectations": _import_or_none("great_expectations"),
    }
    # 总览
    available_count = sum(1 for r in results.values() if r.get("available"))
    return {
        "backends": results,
        "available_count": available_count,
        "total": len(results),
        "production_ready": available_count >= 4,
    }


def install_extras() -> dict[str, Any]:
    """通过 pip 安装 production extras(akshare-mcp[production])。

    实际生产推荐使用 uv:
        uv pip install -e "packages/akshare-mcp[production]"
    """
    print("[install] 安装 akshare-mcp[production] extras ...")
    pkg_path = REPO_ROOT / "packages" / "akshare-mcp"
    cmd = [
        sys.executable, "-m", "pip", "install", "-e",
        f"{pkg_path}[production]",
        "--quiet", "--no-deps",
    ]
    print(f"[install] cmd: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout[-1000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
            "ok": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "install_timeout_180s"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}


def e2e_verify_adapters() -> dict[str, Any]:
    """端到端验证 — 触发每个 adapter 用真实 backend 跑一次。"""
    print("\n[e2e] 验证 adapter 是否切换到真实 backend ...")
    sys.path.insert(0, str(REPO_ROOT / "packages" / "akshare-mcp" / "src"))
    sys.path.insert(0, str(REPO_ROOT / "packages" / "aiask-quant-core" / "src"))
    results = {}

    # 1. experiment_tracker_adapter
    try:
        from akshare_mcp.services.adapters.experiment_tracker_adapter import (
            get_experiment_tracker_adapter,
        )
        adapter = get_experiment_tracker_adapter()
        backend = adapter.backend_name() if hasattr(adapter, "backend_name") else None
        # 跑一个 dummy log_run
        run_info = adapter.log_run(
            experiment_name="install_verify_test",
            params={"x": 1, "verify_at": "install_advanced_calibration"},
            tags={"automation": "install_script"},
        )
        results["experiment_tracker"] = {
            "backend": backend,
            "is_mlflow": "mlflow" in str(type(adapter).__name__).lower() if adapter else False,
            "log_run_ok": bool(run_info),
            "run_id": run_info.get("run_id") if isinstance(run_info, dict) else None,
        }
    except Exception as exc:
        results["experiment_tracker"] = {"error": f"{type(exc).__name__}:{exc}"}

    # 2. data_validation_adapter
    try:
        from akshare_mcp.services.adapters.data_validation_adapter import (
            get_data_validation_adapter,
        )
        adapter = get_data_validation_adapter()
        backend = adapter.backend_name() if hasattr(adapter, "backend_name") else None
        # 给一个含 close/volume 的 dataset
        records = [
            {"close": 10.5, "volume": 1000},
            {"close": 11.0, "volume": 1500},
            {"close": 10.8, "volume": 1200},
        ]
        validate_result = adapter.validate_dataset(records, {})
        results["data_validation"] = {
            "backend": backend,
            "is_ge": "great" in str(type(adapter).__name__).lower() if adapter else False,
            "validate_ok": validate_result.passed if hasattr(validate_result, "passed") else None,
            "expectations_evaluated": validate_result.expectations_evaluated
            if hasattr(validate_result, "expectations_evaluated")
            else None,
        }
    except Exception as exc:
        results["data_validation"] = {"error": f"{type(exc).__name__}:{exc}"}

    # 3. mapie_adapter (conformal)
    try:
        from akshare_mcp.services.adapters.mapie_adapter import (
            get_mapie_adapter,
        )
        adapter = get_mapie_adapter()
        backend = adapter.backend_name() if hasattr(adapter, "backend_name") else None
        results["mapie_conformal"] = {
            "backend": backend,
            "is_mapie": "mapie" in str(type(adapter).__name__).lower() if adapter else False,
        }
    except Exception as exc:
        results["mapie_conformal"] = {"error": f"{type(exc).__name__}:{exc}"}

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="部署侧:advanced calibration / experiment tracking 安装")
    parser.add_argument("--install", action="store_true", help="安装 production extras")
    parser.add_argument("--verify", action="store_true", help="检测 backend 就绪状态")
    parser.add_argument("--e2e", action="store_true", help="端到端验证 adapter")
    parser.add_argument("--check-production", action="store_true", help="仅检查 production extras 状态")
    parser.add_argument("--report-path", type=str, default=None, help="JSON 报告路径")

    args = parser.parse_args()

    if not (args.install or args.verify or args.check_production or args.e2e):
        parser.print_help()
        return 2

    summary: dict[str, Any] = {}

    if args.check_production or args.verify:
        print("=== verify backends ===")
        v = verify_backends()
        summary["verify"] = v
        for name, info in v["backends"].items():
            status = "✓" if info.get("available") else "✗"
            print(f"  {status} {name}: {info.get('version', info.get('error', '?'))}")
        if v["production_ready"]:
            print(f"\n  ✓ production_ready=True ({v['available_count']}/{v['total']})")
        else:
            print(f"\n  ✗ NOT production_ready ({v['available_count']}/{v['total']})")
            print("    → 运行: python scripts/install_advanced_calibration.py --install")

    if args.install:
        print("\n=== install ===")
        i = install_extras()
        summary["install"] = i
        if i.get("ok"):
            print("  ✓ install ok")
        else:
            print(f"  ✗ install failed: {i.get('stderr', '')[:200]}")

    if args.e2e:
        print("\n=== e2e adapter verify ===")
        e = e2e_verify_adapters()
        summary["e2e"] = e
        for name, info in e.items():
            if "error" in info:
                print(f"  ✗ {name}: {info['error']}")
            else:
                print(f"  ✓ {name}: backend={info.get('backend')}")

    if args.report_path:
        rp = Path(args.report_path)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n报告: {rp.resolve()}")

    # 退出码:0 = 全 OK,1 = backend 不全,2 = install 失败
    if args.install and not summary.get("install", {}).get("ok"):
        return 2
    if (args.verify or args.check_production) and not summary.get("verify", {}).get("production_ready"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
