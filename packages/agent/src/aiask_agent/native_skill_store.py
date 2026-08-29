from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .financial_skill_templates import FINANCIAL_SKILL_TEMPLATES
from .native_utils import _limit, _safe_slug
from .numeric import bounded_int
from .paths import aiask_agent_home
from .session_store import now_iso


class SkillStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or aiask_agent_home() / "skills"
        self.archive_root = self.root / ".archive"
        self.backup_root = self.root / ".curator_backups"
        self.usage_path = self.root / ".usage.json"

    def _load_usage(self) -> dict[str, Any]:
        if not self.usage_path.exists():
            return {}
        try:
            loaded = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}

    def _save_usage(self, usage: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.usage_path.write_text(json.dumps(usage, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _metadata_for(self, name: str) -> dict[str, Any]:
        usage = self._load_usage()
        row = dict(usage.get(name) or {})
        row.setdefault("state", "active")
        row.setdefault("pinned", False)
        row.setdefault("view_count", 0)
        row.setdefault("use_count", 0)
        return row

    def _update_metadata(self, name: str, **updates: Any) -> dict[str, Any]:
        skill_name = _safe_slug(name)
        usage = self._load_usage()
        row = dict(usage.get(skill_name) or {})
        row.update(updates)
        row["updated_at"] = time.time()
        usage[skill_name] = row
        self._save_usage(usage)
        return row

    def _active_skill_path(self, name: str) -> Path:
        return self.root / _safe_slug(name) / "SKILL.md"

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            first_line = next((line.strip("# ").strip() for line in content.splitlines() if line.strip()), path.parent.name)
            metadata = self._metadata_for(path.parent.name)
            items.append(
                {
                    "name": path.parent.name,
                    "title": first_line,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "updated_at": path.stat().st_mtime,
                    "state": metadata.get("state") or "active",
                    "pinned": bool(metadata.get("pinned")),
                    "view_count": bounded_int(metadata.get("view_count"), default=0, minimum=0),
                    "use_count": bounded_int(metadata.get("use_count"), default=0, minimum=0),
                    "last_viewed_at": metadata.get("last_viewed_at"),
                    "last_used_at": metadata.get("last_used_at"),
                }
            )
        return items

    def view(self, name: str, *, max_chars: int = 50000) -> dict[str, Any]:
        skill_name = _safe_slug(name)
        path = self._active_skill_path(skill_name)
        if not path.exists():
            raise FileNotFoundError(f"skill not found: {skill_name}")
        content, truncated = _limit(path.read_text(encoding="utf-8", errors="replace"), max_chars)
        metadata = self._metadata_for(skill_name)
        self._update_metadata(
            skill_name,
            **{
                **metadata,
                "state": "active",
                "view_count": bounded_int(metadata.get("view_count"), default=0, minimum=0) + 1,
                "last_viewed_at": time.time(),
            },
        )
        return {"name": skill_name, "path": str(path), "content": content, "truncated": truncated}

    def save(self, name: str, content: str, *, description: str | None = None) -> dict[str, Any]:
        skill_name = _safe_slug(name)
        if not str(content or "").strip():
            raise ValueError("skill content is required")
        path = self._active_skill_path(skill_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = str(content)
        if description and "description:" not in text[:500].lower():
            text = f"---\ndescription: {description}\n---\n\n{text}"
        path.write_text(text, encoding="utf-8")
        metadata = self._metadata_for(skill_name)
        self._update_metadata(
            skill_name,
            **{
                **metadata,
                "state": "active",
                "pinned": bool(metadata.get("pinned")),
                "patch_count": bounded_int(metadata.get("patch_count"), default=0, minimum=0) + 1,
                "last_used_at": time.time(),
                "agent_created": True,
                "archived_at": None,
            },
        )
        return {"name": skill_name, "path": str(path), "bytes": len(text.encode("utf-8"))}

    def pin(self, name: str, pinned: bool = True) -> dict[str, Any]:
        skill_name = _safe_slug(name)
        if not self._active_skill_path(skill_name).exists():
            raise FileNotFoundError(f"skill not found: {skill_name}")
        metadata = self._metadata_for(skill_name)
        metadata["pinned"] = bool(pinned)
        metadata["state"] = "active"
        self._update_metadata(skill_name, **metadata)
        return {"name": skill_name, "pinned": bool(pinned)}

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        skill_name = _safe_slug(name)
        if not self._active_skill_path(skill_name).exists():
            raise FileNotFoundError(f"skill not found: {skill_name}")
        metadata = self._metadata_for(skill_name)
        metadata["state"] = "active" if enabled else "disabled"
        self._update_metadata(skill_name, **metadata)
        return {"name": skill_name, "enabled": bool(enabled), "state": metadata["state"]}

    def archive(self, name: str, *, reason: str | None = None) -> dict[str, Any]:
        skill_name = _safe_slug(name)
        metadata = self._metadata_for(skill_name)
        if metadata.get("pinned"):
            raise PermissionError(f"skill is pinned: {skill_name}")
        path = self.root / skill_name
        if not path.exists():
            raise FileNotFoundError(f"skill not found: {skill_name}")
        self.archive_root.mkdir(parents=True, exist_ok=True)
        dest = self.archive_root / f"{skill_name}-{int(time.time())}"
        shutil.move(str(path), str(dest))
        metadata.update({"state": "archived", "archived_at": time.time(), "archive_reason": reason or "archived"})
        self._update_metadata(skill_name, **metadata)
        return {"name": skill_name, "archived": True, "archive_path": str(dest), "reason": reason or "archived"}

    def restore(self, name: str) -> dict[str, Any]:
        skill_name = _safe_slug(name)
        if self._active_skill_path(skill_name).exists():
            return {"name": skill_name, "restored": False, "reason": "already_active"}
        matches = sorted(self.archive_root.glob(f"{skill_name}-*/SKILL.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            raise FileNotFoundError(f"archived skill not found: {skill_name}")
        src_dir = matches[0].parent
        dest_dir = self.root / skill_name
        shutil.move(str(src_dir), str(dest_dir))
        metadata = self._metadata_for(skill_name)
        metadata.update({"state": "active", "archived_at": None})
        self._update_metadata(skill_name, **metadata)
        return {"name": skill_name, "restored": True, "path": str(dest_dir / "SKILL.md")}

    def backup(self, *, reason: str | None = None) -> dict[str, Any]:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        backup_id = f"backup-{int(time.time())}-{uuid4().hex[:8]}"
        dest = self.backup_root / backup_id
        dest.mkdir(parents=True, exist_ok=False)
        for item in self.root.iterdir() if self.root.exists() else []:
            if item.name == ".curator_backups":
                continue
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        manifest = {"backup_id": backup_id, "reason": reason or "manual", "created_at": time.time()}
        (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return {"backup_id": backup_id, "path": str(dest), "reason": manifest["reason"]}

    def rollback(self, backup_id: str | None = None) -> dict[str, Any]:
        if not self.backup_root.exists():
            raise FileNotFoundError("no skill backups found")
        backups = sorted([p for p in self.backup_root.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
        if backup_id:
            backups = [p for p in backups if p.name == backup_id]
        if not backups:
            raise FileNotFoundError(f"skill backup not found: {backup_id or 'latest'}")
        selected = backups[0]
        self.backup(reason=f"pre-rollback:{selected.name}")
        self.root.mkdir(parents=True, exist_ok=True)
        for item in list(self.root.iterdir()):
            if item.name == ".curator_backups":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        for item in selected.iterdir():
            if item.name == "manifest.json":
                continue
            target = self.root / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        return {"rolled_back": True, "backup_id": selected.name, "root": str(self.root)}

    def audit(self, *, dry_run: bool = True) -> dict[str, Any]:
        skills = self.list()
        issues: list[dict[str, Any]] = []
        seen_titles: dict[str, str] = {}
        for item in skills:
            name = str(item.get("name") or "")
            path = Path(str(item.get("path") or ""))
            content = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            if "description:" not in content[:800].lower():
                issues.append({"severity": "warning", "skill": name, "code": "missing_description", "message": "Skill lacks frontmatter description."})
            if len(content.strip()) < 80:
                issues.append({"severity": "warning", "skill": name, "code": "too_short", "message": "Skill content is too short to be operational."})
            title = str(item.get("title") or "").strip().lower()
            if title and title in seen_titles:
                issues.append({"severity": "info", "skill": name, "code": "duplicate_title", "message": f"Title duplicates {seen_titles[title]}."})
            elif title:
                seen_titles[title] = name
        if not skills:
            issues.append({"severity": "info", "code": "no_skills_installed", "message": "No AIASK native skills are installed in the active skill store."})
        return {
            "skills": skills,
            "issues": issues,
            "issue_count": len(issues),
            "dry_run": bool(dry_run),
            "archive_candidates": [
                item for item in skills
                if not item.get("pinned")
                and bounded_int(item.get("view_count"), default=0, minimum=0) == 0
                and bounded_int(item.get("use_count"), default=0, minimum=0) == 0
            ],
        }

    def install_finance_templates(self, *, overwrite: bool = False) -> dict[str, Any]:
        installed: list[dict[str, Any]] = []
        skipped: list[str] = []
        for name, spec in FINANCIAL_SKILL_TEMPLATES.items():
            if self._active_skill_path(name).exists() and not overwrite:
                skipped.append(name)
                continue
            installed.append(self.save(name, spec["content"], description=spec.get("description")))
        return {"installed": installed, "skipped": skipped, "count": len(installed)}
