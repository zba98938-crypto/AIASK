"""SQLite 适配器 — 策略 AI Mixin (generation experiments / task runs / factory runs)."""

from .strategy_ai_repos.reads import _ReadsMixin
from .strategy_ai_repos.writes import _WritesMixin
from .strategy_ai_repos.factory_runs import _FactoryRunsMixin
from .strategy_ai_repos.dispatch import _DispatchMixin
from .strategy_ai_repos.factory_control import _FactoryControlMixin
from .strategy_ai_repos.topn_scores import _TopnScoresMixin
from .strategy_ai_repos.events import _EventsMixin
from .strategy_ai_repos.theme_graph import _ThemeGraphMixin
from .strategy_ai_repos.lineage import _LineageMixin
from .strategy_ai_repos.outbox import _OutboxMixin
from .strategy_ai_repos.mappers import _MappersMixin


class StrategyAIMixin(
    _ReadsMixin,
    _WritesMixin,
    _FactoryRunsMixin,
    _DispatchMixin,
    _FactoryControlMixin,
    _TopnScoresMixin,
    _EventsMixin,
    _ThemeGraphMixin,
    _LineageMixin,
    _OutboxMixin,
    _MappersMixin,
):
    """Composed strategy-AI storage mixin (fragment loader retired)."""
