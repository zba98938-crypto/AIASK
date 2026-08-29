"""Host Adapter Contract: the surface every host implements to run AIASK.

Phase 5 of the extraction plan. Hermes, a stock MCP client, and the reference
host all need the same small set of capabilities. Writing that set down as a
protocol is what makes "second host" a checkable claim rather than a plan.

The contract is deliberately narrow. A host supplies *integration*: where skills
live, how MCP servers are registered, how a CLI is invoked, and how a policy
decision is surfaced to its user. It supplies no orchestration — that belongs to
Strategy Factory, reached through ``aiask factory``.

Two rules the contract encodes:

* **Side effects are declared, not inferred.** A host must be able to ask whether
  an operation mutates state before running it, so confirmation can happen at the
  host's own trust boundary.
* **Degradation stays visible.** ``render_structured_error`` receives the governed
  envelope, so a host cannot accidentally present a ``degraded`` result as clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


#: Contract version. A host declares which one it implements.
HOST_ADAPTER_PROTOCOL_VERSION = "1.0"

#: What an operation does to state. Hosts gate on this.
SideEffect = Literal["read_only", "mutating", "destructive"]

#: Operations a host may be asked to classify.
KNOWN_OPERATIONS: tuple[str, ...] = (
    "factory.status",
    "factory.preflight",
    "factory.evidence",
    "factory.schedule_template",
    "factory.once",
    "factory.cycle",
    "factory.ops_start",
    "factory.ops_stop",
    "live_trading.enable",
)

#: Default classification. A host may tighten but must not loosen these.
DEFAULT_SIDE_EFFECT_POLICY: dict[str, SideEffect] = {
    "factory.status": "read_only",
    "factory.preflight": "read_only",
    "factory.evidence": "read_only",
    "factory.schedule_template": "read_only",
    "factory.once": "mutating",
    "factory.cycle": "mutating",
    "factory.ops_start": "mutating",
    "factory.ops_stop": "mutating",
    "live_trading.enable": "destructive",
}

_SEVERITY = {"read_only": 0, "mutating": 1, "destructive": 2}


@dataclass(slots=True)
class SkillBundle:
    """A set of skills a host installs, with the metadata needed to gate them."""

    name: str
    version: str
    skills: tuple[str, ...] = ()
    required_mcp: tuple[str, ...] = ()
    protocol_version: str = HOST_ADAPTER_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "skills": list(self.skills),
            "required_mcp": list(self.required_mcp),
            "protocol_version": self.protocol_version,
        }


@dataclass(slots=True)
class McpManifest:
    """An MCP server a host registers.

    ``transport`` is intentionally limited to stdio: it is the only transport
    every host in scope supports without extra infrastructure.
    """

    name: str
    command: tuple[str, ...]
    description: str = ""
    transport: str = "stdio"
    read_only: bool = True
    env: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "description": self.description,
            "transport": self.transport,
            "read_only": self.read_only,
            "env": dict(self.env),
        }


@dataclass(slots=True)
class CapabilityReport:
    """What a host found available. ``degraded_reasons`` keeps gaps visible."""

    host: str
    protocol_version: str = HOST_ADAPTER_PROTOCOL_VERSION
    capabilities: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    degraded_reasons: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Ready means capabilities exist and nothing is degraded."""
        return bool(self.capabilities) and not self.degraded_reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "protocol_version": self.protocol_version,
            "capabilities": list(self.capabilities),
            "mcp_servers": list(self.mcp_servers),
            "skills": list(self.skills),
            "degraded_reasons": list(self.degraded_reasons),
            "ready": self.ready,
        }


@runtime_checkable
class HostAdapter(Protocol):
    """What a host must implement to run AIASK.

    Every method returns data rather than printing, so a host can render results
    in whatever form suits it while the decisions stay uniform.
    """

    def install_skill_bundle(self, bundle: SkillBundle) -> dict[str, Any]:
        """Install or link a skill bundle. Must be idempotent."""
        ...

    def register_mcp(self, manifest: McpManifest) -> dict[str, Any]:
        """Register an MCP server. Must be idempotent."""
        ...

    def discover_capabilities(self) -> CapabilityReport:
        """Report what is installed and usable, including what is missing."""
        ...

    def invoke_aiask_cli(
        self,
        argv: list[str],
        *,
        timeout: float = 7200.0,
    ) -> dict[str, Any]:
        """Run ``aiask ...`` and return its governed envelope.

        The host owns process spawning and environment safety; it must not
        reimplement the command's behaviour.
        """
        ...

    def map_side_effect_policy(self, operation: str) -> SideEffect:
        """Classify an operation so the host can gate it before running."""
        ...

    def render_structured_error(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Turn a failed or degraded envelope into host-presentable form.

        Must preserve ``status`` and ``degraded_reasons``: a degraded result may
        never be rendered as a clean success.
        """
        ...


def default_side_effect(operation: str) -> SideEffect:
    """Baseline classification. Unknown operations are treated as mutating.

    Fails closed: an unrecognised operation is assumed to change state, so a new
    verb cannot slip past a host's confirmation gate by being unclassified.
    """
    return DEFAULT_SIDE_EFFECT_POLICY.get(str(operation or ""), "mutating")


def assert_policy_not_loosened(operation: str, declared: SideEffect) -> None:
    """Raise if a host classified an operation as less dangerous than baseline."""
    baseline = default_side_effect(operation)
    if _SEVERITY.get(declared, 1) < _SEVERITY.get(baseline, 1):
        raise ValueError(
            f"host classified {operation!r} as {declared!r}, weaker than the "
            f"required {baseline!r}; a host may tighten policy but not loosen it"
        )


def missing_adapter_methods(adapter: Any) -> list[str]:
    """Contract methods ``adapter`` does not implement."""
    required = (
        "install_skill_bundle",
        "register_mcp",
        "discover_capabilities",
        "invoke_aiask_cli",
        "map_side_effect_policy",
        "render_structured_error",
    )
    return sorted(name for name in required if not callable(getattr(adapter, name, None)))


def verify_host_adapter(adapter: Any) -> dict[str, Any]:
    """Check an adapter against the contract without executing side effects."""
    missing = missing_adapter_methods(adapter)
    return {
        "protocol_version": HOST_ADAPTER_PROTOCOL_VERSION,
        "adapter_type": type(adapter).__name__,
        "missing_methods": missing,
        "conforms": not missing,
    }


__all__ = [
    "DEFAULT_SIDE_EFFECT_POLICY",
    "HOST_ADAPTER_PROTOCOL_VERSION",
    "KNOWN_OPERATIONS",
    "CapabilityReport",
    "HostAdapter",
    "McpManifest",
    "SideEffect",
    "SkillBundle",
    "assert_policy_not_loosened",
    "default_side_effect",
    "missing_adapter_methods",
    "verify_host_adapter",
]
