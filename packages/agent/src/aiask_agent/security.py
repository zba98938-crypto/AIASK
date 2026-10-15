from __future__ import annotations

import json
import os
import re
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .general_tools import WorkspaceGuard
from .tools.policy import ToolPolicy
import logging

logger = logging.getLogger(__name__)


_ADVISORIES_PATH = Path(__file__).resolve().parent / "data" / "known_advisories.json"


def _parse_version(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[._\-+]", str(value or "").strip()):
        match = re.match(r"^(\d+)", chunk)
        if not match:
            break
        parts.append(int(match.group(1)))
    return tuple(parts) if parts else (0,)


def _cmp_versions(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    length = max(len(a), len(b))
    a_padded = a + (0,) * (length - len(a))
    b_padded = b + (0,) * (length - len(b))
    if a_padded < b_padded:
        return -1
    if a_padded > b_padded:
        return 1
    return 0


def _matches_constraint(version: str, constraint: str) -> bool:
    expr = str(constraint or "").strip()
    if not expr or not version:
        return False
    current = _parse_version(version)
    for clause in expr.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(r"^(<=|>=|==|!=|<|>)\s*([0-9][0-9A-Za-z._\-+]*)\s*$", clause)
        if not match:
            return False
        op, target_raw = match.group(1), match.group(2)
        target = _parse_version(target_raw)
        diff = _cmp_versions(current, target)
        if op == "<" and not (diff < 0):
            return False
        if op == "<=" and not (diff <= 0):
            return False
        if op == ">" and not (diff > 0):
            return False
        if op == ">=" and not (diff >= 0):
            return False
        if op == "==" and not (diff == 0):
            return False
        if op == "!=" and not (diff != 0):
            return False
    return True


def _ledger_defect(ledger: dict[str, Any]) -> str | None:
    """Return a machine-readable defect code when the ledger is unusable.

    A missing, unparsable or structurally invalid ledger must never be reported
    as a clean scan: the scanner would claim "passed" while checking nothing.
    """

    if ledger.get("missing_ledger"):
        return "ledger_missing"
    if ledger.get("parse_error"):
        return "ledger_parse_error"
    if not isinstance(ledger.get("advisories"), list):
        return "ledger_schema_invalid"
    try:
        schema_version = int(ledger.get("schema_version") or 0)
    except (TypeError, ValueError):
        return "ledger_schema_invalid"
    if schema_version < 1:
        return "ledger_schema_invalid"
    for advisory in ledger["advisories"]:
        if not isinstance(advisory, dict):
            return "ledger_schema_invalid"
        if not str(advisory.get("package") or "").strip():
            return "ledger_schema_invalid"
        if not str(advisory.get("advisory_id") or "").strip():
            return "ledger_schema_invalid"
        constraints = advisory.get("vulnerable_versions")
        if not isinstance(constraints, list) or not constraints:
            return "ledger_schema_invalid"
        if any(not str(expr or "").strip() for expr in constraints):
            return "ledger_schema_invalid"
    return None


def _load_advisories() -> dict[str, Any]:
    try:
        raw = _ADVISORIES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema_version": 0, "advisories": [], "missing_ledger": True}
    except OSError as exc:
        return {"schema_version": 0, "advisories": [], "parse_error": str(exc)}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"schema_version": 0, "advisories": [], "parse_error": str(exc)}
    if not isinstance(loaded, dict):
        return {"schema_version": 0, "advisories": [], "parse_error": "ledger root must be a JSON object"}
    return loaded


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic_secret_assignment", re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?[^'\"\s]{8,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

_LEDGER_DEFECT_MESSAGES: dict[str, str] = {
    "ledger_missing": "Offline advisory ledger is missing; dependency advisory results are not trustworthy.",
    "ledger_parse_error": "Offline advisory ledger could not be parsed; dependency advisory results are not trustworthy.",
    "ledger_schema_invalid": "Offline advisory ledger failed schema validation; dependency advisory results are not trustworthy.",
}

PROTECTED_PATH_PARTS = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".gcloud",
    ".kube",
    ".docker",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".env",
}


class SecurityScanner:
    def __init__(self, *, policy: ToolPolicy | None = None, env: dict[str, str] | None = None) -> None:
        self.policy = policy
        self.env = dict(os.environ if env is None else env)

    def status(self) -> dict[str, Any]:
        ledger = _load_advisories()
        defect = _ledger_defect(ledger)
        return {
            "object": "aiask.security_status",
            "implemented": True,
            "checks": [
                "secret_pattern_scan",
                "protected_credential_path_scan",
                "private_url_scan",
                "environment_redaction_readiness",
                "dependency_advisory",
            ],
            "protected_path_parts": sorted(PROTECTED_PATH_PARTS),
            "ssrf_private_url_default_block": self.env.get("AIASK_AGENT_ALLOW_PRIVATE_WEB", "").strip().lower() not in {"1", "true", "yes", "on"},
            "secrets_redacted": True,
            "dependency_advisory": {
                "configured": defect is None,
                "ledger_path": str(_ADVISORIES_PATH),
                "ledger_version": ledger.get("schema_version"),
                "advisory_count": len(list(ledger.get("advisories") or [])),
                "ledger_last_reviewed": ledger.get("last_reviewed"),
                "status": "degraded" if defect else "implemented",
                "error_code": defect,
                "error": _LEDGER_DEFECT_MESSAGES.get(defect) if defect else None,
                "degraded": defect is not None,
            },
            "status": "degraded" if defect else "implemented",
        }

    def scan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        mode = str(arguments.get("action") or arguments.get("mode") or "secret_scan").strip().lower() or "secret_scan"
        if mode in {"dependency_advisory", "dependencies", "deps"}:
            return self.dependency_advisory(arguments)
        text = str(arguments.get("text") or "")
        path = str(arguments.get("path") or "").strip()
        url = str(arguments.get("url") or "").strip()
        include_env = bool(arguments.get("include_env", False))
        findings: list[dict[str, Any]] = []
        scanned: dict[str, Any] = {"text": bool(text), "path": bool(path), "url": bool(url), "environment_keys": False}

        if text:
            findings.extend(self._scan_text(text, source="text"))
        if path:
            findings.extend(self._scan_path(path))
        if url:
            findings.extend(self._scan_url(url))
        if include_env:
            scanned["environment_keys"] = True
            findings.extend(self._scan_env_keys())

        severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
        max_severity = max((severity_rank.get(str(item.get("severity")), 0) for item in findings), default=0)
        status = "blocked" if max_severity >= 4 else "warning" if findings else "passed"
        return {
            "scanned": scanned,
            "findings": findings,
            "finding_count": len(findings),
            "status": status,
            "secrets_redacted": True,
        }

    def _scan_text(self, text: str, *, source: str) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for code, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                findings.append(
                    {
                        "severity": "critical" if code == "private_key_block" else "high",
                        "code": code,
                        "source": source,
                        "span": [match.start(), match.end()],
                        "preview": self._preview(text, match.start(), match.end()),
                        "message": "Potential secret material detected; value redacted.",
                    }
                )
        return findings

    def _scan_path(self, raw_path: str) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        path = Path(raw_path).expanduser()
        path_parts = set(path.parts)
        protected = sorted(part for part in PROTECTED_PATH_PARTS if part in path_parts or path.name == part)
        if protected:
            findings.append(
                {
                    "severity": "high",
                    "code": "protected_credential_path",
                    "source": "path",
                    "path": str(path),
                    "protected_parts": protected,
                    "message": "Path points at a protected credential/config location.",
                }
            )
        if self.policy is not None:
            try:
                WorkspaceGuard(self.policy).resolve(str(path), must_exist=False)
            except Exception as exc:
                findings.append(
                    {
                        "severity": "medium",
                        "code": "outside_workspace_root",
                        "source": "path",
                        "path": str(path),
                        "message": str(exc),
                    }
                )
        if path.exists() and path.is_file():
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                findings.append({"severity": "low", "code": "file_unreadable", "source": "path", "path": str(path), "message": str(exc)})
            else:
                findings.extend(self._scan_text(raw[:200000], source=str(path)))
        return findings

    def _scan_url(self, raw_url: str) -> list[dict[str, Any]]:
        parsed = urlparse(raw_url)
        findings: list[dict[str, Any]] = []
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            findings.append({"severity": "medium", "code": "invalid_external_url", "source": "url", "url": raw_url, "message": "URL must be absolute http(s)."})
        host = parsed.hostname or ""
        if host in {"localhost", "127.0.0.1", "::1"} or host.startswith("10.") or host.startswith("192.168.") or re.match(r"^172\.(1[6-9]|2\d|3[0-1])\.", host):
            findings.append({"severity": "high", "code": "private_or_loopback_url", "source": "url", "url": raw_url, "message": "Private and loopback URLs are SSRF-sensitive."})
        if parsed.username or parsed.password:
            findings.append({"severity": "high", "code": "url_embedded_credentials", "source": "url", "url": raw_url, "message": "URL contains embedded credentials."})
        return findings

    def _scan_env_keys(self) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for key in sorted(self.env):
            upper = key.upper()
            if any(token in upper for token in ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "AUTH", "CREDENTIAL")):
                findings.append(
                    {
                        "severity": "info",
                        "code": "sensitive_env_key_present",
                        "source": "environment",
                        "key": key,
                        "message": "Sensitive environment key is present; value is not exposed.",
                    }
                )
        return findings

    @staticmethod
    def _preview(text: str, start: int, end: int) -> str:
        left = max(0, start - 8)
        right = min(len(text), end + 8)
        return f"{text[left:start]}[REDACTED]{text[end:right]}"

    def dependency_advisory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ledger = _load_advisories()
        defect = _ledger_defect(ledger)
        advisories = list(ledger.get("advisories") or []) if defect is None else []
        explicit_packages = arguments.get("packages")
        explicit_map: dict[str, str] = {}
        if isinstance(explicit_packages, dict):
            explicit_map = {str(k).strip().lower(): str(v).strip() for k, v in explicit_packages.items() if str(k).strip()}
        elif isinstance(explicit_packages, list):
            for item in explicit_packages:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("package") or "").strip().lower()
                    version = str(item.get("version") or "").strip()
                    if name:
                        explicit_map[name] = version
        include_loaded = bool(arguments.get("include_loaded", True))
        include_installed = bool(arguments.get("include_installed", True))

        observed: dict[str, dict[str, Any]] = {}

        if explicit_map:
            for name, version in explicit_map.items():
                observed[name] = {"package": name, "version": version, "source": "explicit"}

        if include_loaded:
            seen_modules = sorted({mod.split(".", 1)[0] for mod in list(sys.modules.keys()) if mod and not mod.startswith("_")})
            for module_name in seen_modules:
                key = module_name.lower()
                if key in observed:
                    continue
                version = self._safe_version(module_name)
                if version:
                    observed[key] = {"package": module_name, "version": version, "source": "loaded_module"}

        if include_installed:
            try:
                distributions = list(importlib_metadata.distributions())
            except Exception:
                distributions = []
            for dist in distributions:
                try:
                    name = str(dist.metadata.get("Name") or "").strip()
                    version = str(dist.version or "").strip()
                except Exception as _swallowed:
                    logger.warning("packages/agent/src/aiask_agent/security.py:348: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    continue
                if not name:
                    continue
                key = name.lower()
                if key in observed and observed[key].get("source") != "explicit":
                    observed[key]["version"] = observed[key].get("version") or version
                    continue
                if key not in observed:
                    observed[key] = {"package": name, "version": version, "source": "installed"}

        findings: list[dict[str, Any]] = []
        for advisory in advisories:
            target_pkg = str(advisory.get("package") or "").strip().lower()
            if not target_pkg or target_pkg not in observed:
                continue
            current = observed[target_pkg]
            current_version = str(current.get("version") or "").strip()
            if not current_version:
                continue
            constraints = list(advisory.get("vulnerable_versions") or [])
            matched = any(_matches_constraint(current_version, expr) for expr in constraints)
            if not matched:
                continue
            findings.append(
                {
                    "package": current.get("package"),
                    "installed_version": current_version,
                    "vulnerable_versions": constraints,
                    "advisory_id": advisory.get("advisory_id"),
                    "severity": advisory.get("severity") or "medium",
                    "summary": advisory.get("summary") or "",
                    "fixed_in": advisory.get("fixed_in"),
                    "category": advisory.get("category") or "security",
                    "source": current.get("source"),
                }
            )

        severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
        max_severity = max((severity_rank.get(str(item.get("severity")), 0) for item in findings), default=0)
        if defect is not None:
            # An unusable ledger means nothing was actually checked. Reporting
            # "passed" here would be a false clean bill of health.
            status = "degraded"
        elif max_severity >= 4:
            status = "blocked"
        elif findings:
            status = "warning"
        else:
            status = "passed"
        return {
            "object": "aiask.dependency_advisory",
            "configured": defect is None,
            "ledger_path": str(_ADVISORIES_PATH),
            "ledger_version": ledger.get("schema_version"),
            "ledger_last_reviewed": ledger.get("last_reviewed"),
            "advisory_count": len(advisories),
            "scanned_count": len(observed),
            "findings": findings,
            "finding_count": len(findings),
            "status": status,
            "degraded": defect is not None,
            "error_code": defect,
            "error": _LEDGER_DEFECT_MESSAGES.get(defect) if defect else None,
            "secrets_redacted": True,
            "lazy_supply_chain_check": True,
        }

    @staticmethod
    def _safe_version(module_name: str) -> str:
        try:
            return str(importlib_metadata.version(module_name))
        except importlib_metadata.PackageNotFoundError as _swallowed:
            logger.warning("packages/agent/src/aiask_agent/security.py:420: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return ""
        except Exception as _swallowed:
            logger.warning("packages/agent/src/aiask_agent/security.py:422: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return ""

