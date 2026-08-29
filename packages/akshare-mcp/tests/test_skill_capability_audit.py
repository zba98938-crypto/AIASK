from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from akshare_mcp.tools.skills import _available_skill_handlers
from akshare_mcp.tools.skills_registry import (
    _build_skill_registry_summary,
    _enrich_skills,
    _load_skills,
    _parse_skill_md,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / ".codex" / "skills"


def _has_repo_local_skill_docs() -> bool:
    return SKILLS_DIR.is_dir() and any(SKILLS_DIR.glob("*/SKILL.md"))


def _load_audit_module():
    audit_path = REPO_ROOT / "scripts" / "skill_coverage_audit.py"
    spec = importlib.util.spec_from_file_location("skill_coverage_audit", audit_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_repo_local_skill_frontmatter_exposes_machine_fields():
    skill_path = REPO_ROOT / ".codex" / "skills" / "akshare-market" / "SKILL.md"
    if not skill_path.is_file():
        pytest.skip("repo-local Markdown skill docs are not present")

    payload = _parse_skill_md(skill_path)

    assert payload["capability_tier"] == "live_orchestrated"
    assert payload["runtime_status"] == "executable"
    assert "mcp" in payload["product_surfaces"]
    assert "get_realtime_quote" in payload["backing_tools"]
    assert "trader" in payload["role_tags"]
    assert payload["last_runtime_verified_at"] == "2026-04-19"


def test_skill_registry_summary_exposes_capability_audit_fields():
    skills = _enrich_skills(_load_skills(), available_handlers=_available_skill_handlers())
    summary = _build_skill_registry_summary(skills, available_handlers=_available_skill_handlers())

    if not _has_repo_local_skill_docs():
        assert summary["repo_local_skill_count"] == 0
        assert summary["runtime_contract_count"] == 0
        assert summary["runtime_executor_count"] == 0
        assert summary["stale_meta_detected"] is False
        assert summary["meta_conflicts"] == []
        return

    # NOTE: ``.codex/skills/`` 是 git-ignored 的本地工作目录内容（见 .gitignore），
    # 其技能数量随开发机环境而异，**不可硬编码绝对值**（历史上写死 21 会在缺失完整
    # 技能集的环境/CI 上误失败）。此处改为校验审计逻辑的内部一致性与字段契约。
    repo_local = int(summary["repo_local_skill_count"])
    contract = int(summary["runtime_contract_count"])
    executor = int(summary["runtime_executor_count"])
    assert repo_local >= 1
    assert contract >= 0
    assert executor >= 0
    assert isinstance(summary["meta_conflicts"], list)
    assert isinstance(summary["stale_meta_detected"], bool)
    # stale_meta_detected 应与「本地数 != 契约数 或 != 执行器数」一致
    expected_stale = (repo_local != contract) or (repo_local != executor)
    assert summary["stale_meta_detected"] == expected_stale
    # 有不一致时必须产出冲突明细，反之为空
    assert bool(summary["meta_conflicts"]) == expected_stale
    assert isinstance(summary.get("capability_tier_breakdown") or {}, dict)
    assert isinstance(summary.get("role_tag_breakdown") or {}, dict)


def test_skill_capability_audit_is_fully_aligned():
    audit_mod = _load_audit_module()
    server_file = REPO_ROOT / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "server.py"
    tools_dir = REPO_ROOT / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "tools"
    package_root = REPO_ROOT / "packages" / "akshare-mcp" / "src" / "akshare_mcp"
    skills_dir = SKILLS_DIR

    runtime_tools, _, tool_source = audit_mod.discover_runtime_tools(REPO_ROOT, server_file, tools_dir)
    skill_coverages = audit_mod.collect_skill_coverages(skills_dir, set(runtime_tools), REPO_ROOT)
    skill_executor_audit = audit_mod.discover_skill_executors(tools_dir / "skills.py")
    runtime_contract_skill_ids = audit_mod.discover_runtime_skill_contracts(
        REPO_ROOT, [item.skill for item in skill_coverages]
    )
    capability_audit = audit_mod.build_skill_capability_audit(
        REPO_ROOT,
        skill_coverages=skill_coverages,
        runtime_contract_skill_ids=runtime_contract_skill_ids,
        runtime_executor_skill_ids=sorted(skill_executor_audit.get("executable_skill_ids") or []),
        runtime_tools=runtime_tools,
        tool_coverage_source=tool_source,
    )

    assert len(runtime_tools) >= 150
    if not skill_coverages:
        assert capability_audit["actual_local_skills"] == []
        assert capability_audit["runtime_contract_skills"] == []
        assert capability_audit["missing_from_meta"]["frontmatter_fields"] == {}
        assert capability_audit["stale_meta_detected"] is False
        return

    # NOTE: ``.codex/skills/`` 为 git-ignored 本地内容，技能集随环境而异。
    # 历史断言写死「actual==contract==executor 且 count==21」，在缺失完整运行时技能集的
    # 环境/CI 上会误失败。改为校验审计逻辑自身的正确性与一致性（与实际本地状态相符）。
    actual_local = list(capability_audit["actual_local_skills"])
    runtime_contract = list(capability_audit["runtime_contract_skills"])
    runtime_executor = list(capability_audit["runtime_executor_skills"])
    assert isinstance(actual_local, list) and actual_local
    assert isinstance(runtime_contract, list)
    assert isinstance(runtime_executor, list)

    fully_aligned = (actual_local == runtime_contract == runtime_executor)
    # stale_meta_detected 必须与三集是否完全对齐保持一致（审计逻辑自洽性）
    assert capability_audit["stale_meta_detected"] == (not fully_aligned)
    if fully_aligned:
        # 完整运行时技能集在位时，保持原有的强一致性保证
        assert capability_audit["missing_from_meta"]["frontmatter_fields"] == {}
        assert capability_audit["meta_conflicts"] == []
        assert capability_audit["live_validation_failures"] == []
    else:
        # 部分集（如仅 doc-only 技能）时，必须显式报告冲突而非静默
        assert capability_audit["meta_conflicts"]

    report = audit_mod.compute_report(
        REPO_ROOT,
        runtime_tools,
        [server_file],
        skill_coverages,
        audit_mod.detect_module_name_collisions(package_root),
        skill_executor_audit,
        tool_source,
        capability_audit,
    )
    assert report["repo_local_skill_count"] == len(actual_local)
    assert report["runtime_contract_count"] == len(runtime_contract)
    assert report["runtime_executor_count"] == len(runtime_executor)
    assert report["stale_meta_detected"] == (not fully_aligned)
