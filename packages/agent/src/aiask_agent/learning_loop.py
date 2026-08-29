from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from .approvals import ApprovalStore, public_approval
from .memory import FinancialMemoryStore
from .native_capabilities import SkillStore
from .session_store import AgentSessionStore, now_iso


class LearningLoopStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def _db_path(self) -> Path:
        if self.path is not None:
            return self.path
        from .paths import default_state_db_path

        return default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        path = self._db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS learning_proposals (
                proposal_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                source_run_id TEXT,
                source_session_id TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        return conn

    def create(
        self,
        *,
        kind: str,
        title: str,
        content: str,
        source_run_id: str | None = None,
        source_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        proposal_id = f"learn_{uuid4().hex}"
        ts = now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO learning_proposals
                    (proposal_id, kind, title, content, status, source_run_id, source_session_id, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    kind,
                    title,
                    content,
                    "pending",
                    source_run_id,
                    source_session_id,
                    json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True),
                    ts,
                    ts,
                ),
            )
            conn.commit()
        item = self.get(proposal_id)
        assert item is not None
        return item

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM learning_proposals WHERE proposal_id = ?", (str(proposal_id or "").strip(),)).fetchone()
        return self._row(row)

    def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        values: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            values.append(status)
        values.append(max(1, min(int(limit or 100), 1000)))
        with closing(self._connect()) as conn:
            rows = conn.execute(f"SELECT * FROM learning_proposals {where} ORDER BY created_at DESC LIMIT ?", tuple(values)).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def update_status(self, proposal_id: str, status: str, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        item = self.get(proposal_id)
        if item is None:
            return None
        next_meta = dict(item.get("metadata") or {})
        next_meta.update(dict(metadata or {}))
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE learning_proposals SET status = ?, metadata_json = ?, updated_at = ? WHERE proposal_id = ?",
                (status, json.dumps(next_meta, ensure_ascii=False, sort_keys=True), now_iso(), proposal_id),
            )
            conn.commit()
        return self.get(proposal_id)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
        except Exception:
            item["metadata"] = {}
        return item


class LearningLoop:
    def __init__(self, *, session_store: AgentSessionStore, state_path: Path | None = None) -> None:
        self.session_store = session_store
        self.state_path = state_path or session_store.path
        self.store = LearningLoopStore(self.state_path)
        self.memories = FinancialMemoryStore(self.state_path)
        self.skills = SkillStore()
        self.approvals = ApprovalStore(self.state_path)

    def status(self) -> dict[str, Any]:
        pending = self.store.list(status="pending", limit=100)
        return {
            "object": "aiask.learning_status",
            "enabled": os.getenv("AIASK_LEARNING_DISABLED", "").strip().lower() not in {"1", "true", "yes", "on"},
            "auto_apply_skills": os.getenv("AIASK_LEARNING_AUTO_APPLY_SKILLS", "").strip().lower() in {"1", "true", "yes", "on"},
            "pending_count": len(pending),
            "pending": pending,
            "memory_provider": "aiask_builtin",
            "skill_provider": "aiask_skill_store",
        }

    def observe_run(self, result: Any) -> list[dict[str, Any]]:
        if os.getenv("AIASK_LEARNING_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            return []
        content = str(getattr(result, "content", "") or "").strip()
        if not content:
            return []
        created: list[dict[str, Any]] = []
        if len(content) >= 80:
            memory = self.memories.add(
                content=content[:2000],
                user_id=None,
                research_topic="agent_run_summary",
            )
            created.append({"kind": "memory", "memory": memory})
        if len(getattr(result, "tool_calls", []) or []) >= 2:
            proposal = self.store.create(
                kind="skill",
                title=f"Skill candidate from {getattr(result, 'run_id', '')}",
                content=self._skill_content(result),
                source_run_id=getattr(result, "run_id", None),
                source_session_id=getattr(result, "session_id", None),
                metadata={"tool_call_count": len(getattr(result, "tool_calls", []) or [])},
            )
            if os.getenv("AIASK_LEARNING_AUTO_APPLY_SKILLS", "").strip().lower() in {"1", "true", "yes", "on"}:
                self.apply(proposal["proposal_id"])
                proposal = self.store.get(proposal["proposal_id"]) or proposal
            else:
                approval = self.approvals.create(
                    tool_name="agent_learning_apply",
                    action="learning_apply",
                    arguments={"proposal_id": proposal["proposal_id"]},
                    reason="Skill changes require approval unless AIASK_LEARNING_AUTO_APPLY_SKILLS=1",
                )
                proposal = self.store.update_status(proposal["proposal_id"], "pending_approval", {"approval": public_approval(approval)}) or proposal
            created.append({"kind": "proposal", "proposal": proposal})
        return created

    def review(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list(status=status, limit=limit)

    def apply(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.store.get(proposal_id)
        if proposal is None:
            raise FileNotFoundError(f"learning proposal not found: {proposal_id}")
        if proposal["kind"] == "skill":
            skill = self.skills.save(proposal["title"], proposal["content"], description="AIASK learned skill")
            return self.store.update_status(proposal_id, "applied", {"skill": skill}) or proposal
        if proposal["kind"] == "memory":
            memory = self.memories.add(content=proposal["content"], research_topic="learning_loop")
            return self.store.update_status(proposal_id, "applied", {"memory": memory}) or proposal
        return self.store.update_status(proposal_id, "skipped", {"reason": "unsupported proposal kind"}) or proposal

    def reflect_skill(self, *, name: str, observation: str) -> dict[str, Any]:
        if not name or not observation:
            raise ValueError("name and observation are required")
        proposal = self.store.create(
            kind="skill",
            title=f"{name}-reflection",
            content=f"# {name} Reflection\n\n{observation.strip()}\n",
            metadata={"source": "agent_skill_reflect", "name": name},
        )
        return proposal

    @staticmethod
    def _skill_content(result: Any) -> str:
        calls = getattr(result, "tool_calls", []) or []
        lines = ["# Learned Workflow", "", "## Trigger", "", "Use this when a similar multi-tool workflow appears.", "", "## Observed Tool Sequence", ""]
        for call in calls:
            lines.append(f"- {call.get('name')}")
        lines.extend(["", "## Outcome", "", str(getattr(result, "content", "") or "")[:2000]])
        return "\n".join(lines).strip() + "\n"
