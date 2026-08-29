"""strategy_mgr_crud package facade — re-exports handle_* (split from single module)."""

from . import _support as _support
from . import _personal_support as _personal_support

# Preserve the original single-module surface: re-export every public + private
# helper name so existing `from ...strategy_mgr_crud import _helper` imports keep working.
for _mod in (_support, _personal_support):
    globals().update(
        {name: getattr(_mod, name) for name in dir(_mod) if not name.startswith("__")}
    )

from .handlers_catalog import (
    handle_archive,
    handle_create,
    handle_detail,
    handle_events,
    handle_help,
    handle_list,
    handle_my_strategies,
    handle_my_subscriptions,
    handle_publish,
    handle_review,
    handle_review_report,
    handle_subscribe,
    handle_unsubscribe,
    handle_update_metrics,
)
from .handlers_personal import (
    handle_ai_optimize_personal_strategy,
    handle_capabilities,
    handle_daily_snapshot,
    handle_daily_snapshots,
    handle_delete_personal_strategy,
    handle_fork_strategy,
    handle_get_forward_returns,
    handle_get_signal_stats,
    handle_get_signals,
    handle_paper_session_get,
    handle_paper_session_get_or_create,
    handle_personal_strategy_context,
    handle_personal_strategy_suggestions,
    handle_rank,
    handle_update_strategy,
)

__all__ = [
    "handle_ai_optimize_personal_strategy",
    "handle_archive",
    "handle_capabilities",
    "handle_create",
    "handle_daily_snapshot",
    "handle_daily_snapshots",
    "handle_delete_personal_strategy",
    "handle_detail",
    "handle_events",
    "handle_fork_strategy",
    "handle_get_forward_returns",
    "handle_get_signal_stats",
    "handle_get_signals",
    "handle_help",
    "handle_list",
    "handle_my_strategies",
    "handle_my_subscriptions",
    "handle_paper_session_get",
    "handle_paper_session_get_or_create",
    "handle_personal_strategy_context",
    "handle_personal_strategy_suggestions",
    "handle_publish",
    "handle_rank",
    "handle_review",
    "handle_review_report",
    "handle_subscribe",
    "handle_unsubscribe",
    "handle_update_metrics",
    "handle_update_strategy",
]
