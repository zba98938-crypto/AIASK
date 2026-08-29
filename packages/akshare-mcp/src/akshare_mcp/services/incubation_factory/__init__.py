"""孵化工厂 · 独立运行模块。

孵化工厂的两大核心使命：
1. 审查策略工厂生成的策略的质量与准确性
2. 体现策略工厂生成策略的命中率，从而体现 AI 生成交易策略的胜率

孵化工厂作为独立进程运行，与策略工厂通过数据库解耦。
"""

from .runner import IncubationFactoryRunner, get_incubation_factory_runner
from .intake import IncubationIntake
from .signal_generator import SignalGenerator
from .forward_verifier import ForwardVerifier
from .metrics_recorder import MetricsRecorder
from .hit_rate_reporter import HitRateReporter
from .feedback_writer import FeedbackWriter
from .trade_prediction_verifier import IntradayReplayService, TradePredictionDailyVerifier

__all__ = [
    "IncubationFactoryRunner",
    "get_incubation_factory_runner",
    "IncubationIntake",
    "SignalGenerator",
    "ForwardVerifier",
    "MetricsRecorder",
    "HitRateReporter",
    "FeedbackWriter",
    "IntradayReplayService",
    "TradePredictionDailyVerifier",
]
