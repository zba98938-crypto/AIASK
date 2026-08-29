"""
Alpha 挖掘模块（本地规则版）

使用本地规则生成和评估因子候选：
- 因子候选生成
- 因子有效性评估
- Alpha衰减检测
- 因子组合优化

Author: AKShare MCP Server
Version: 2.1
"""

import numpy as np
import pandas as pd
import re
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

logger = logging.getLogger(__name__)


# ── P2-1: 可解释文本信号管线 ──────────────────────────────

class TextSignalPipeline:
    """基于关键词词典的可解释文本信号管线。"""

    # 金融情绪词典：{关键词: 权重}，正=利好，负=利空
    SENTIMENT_DICT: Dict[str, float] = {
        # 利好
        "利好": 1.0, "上涨": 0.8, "突破": 0.7, "增长": 0.9, "盈利": 0.8,
        "超预期": 1.2, "创新高": 1.0, "放量": 0.6, "回购": 0.7, "增持": 0.8,
        "分红": 0.5, "扭亏": 1.0, "中标": 0.7, "签约": 0.6, "获批": 0.7,
        "涨停": 0.9, "大涨": 0.8, "景气": 0.6, "复苏": 0.7, "加速": 0.5,
        # 利空
        "利空": -1.0, "下跌": -0.8, "跌破": -0.7, "亏损": -0.9, "减持": -0.8,
        "暴跌": -1.2, "跌停": -0.9, "缩量": -0.4, "违规": -1.0, "处罚": -0.9,
        "退市": -1.5, "爆雷": -1.3, "质押": -0.5, "诉讼": -0.6, "下滑": -0.7,
        "萎缩": -0.6, "衰退": -0.8, "风险": -0.5, "警示": -0.6, "ST": -1.0,
    }

    @classmethod
    def score_text(cls, text: str) -> Dict[str, Any]:
        """对单条文本打分，返回分数和命中关键词证据。"""
        if not text:
            return {"score": 0.0, "hits": []}
        score = 0.0
        hits: List[Dict[str, Any]] = []
        for kw, weight in cls.SENTIMENT_DICT.items():
            count = len(re.findall(re.escape(kw), text))
            if count > 0:
                contrib = weight * count
                score += contrib
                hits.append({"keyword": kw, "weight": weight, "count": count, "contrib": round(contrib, 3)})
        return {"score": round(score, 4), "hits": hits}

    @classmethod
    def aggregate_signals(cls, headlines: List[str]) -> Dict[str, Any]:
        """聚合多条新闻的文本信号，输出可解释的综合评分。"""
        if not headlines:
            return {"signal_score": 0.0, "n_articles": 0, "evidence": [], "sentiment": "neutral"}
        details = [cls.score_text(h) for h in headlines]
        scores = [d["score"] for d in details]
        avg_score = float(np.mean(scores)) if scores else 0.0
        # 收集 top 命中证据
        all_hits: Dict[str, float] = {}
        for d in details:
            for h in d["hits"]:
                all_hits[h["keyword"]] = all_hits.get(h["keyword"], 0.0) + h["contrib"]
        top_evidence = sorted(all_hits.items(), key=lambda x: abs(x[1]), reverse=True)[:10]

        sentiment = "bullish" if avg_score > 0.3 else ("bearish" if avg_score < -0.3 else "neutral")
        return {
            "signal_score": round(avg_score, 4),
            "n_articles": len(headlines),
            "sentiment": sentiment,
            "positive_count": sum(1 for s in scores if s > 0),
            "negative_count": sum(1 for s in scores if s < 0),
            "evidence": [{"keyword": k, "total_contrib": round(v, 3)} for k, v in top_evidence],
        }


class LLMAlphaMiner:
    """Alpha 挖掘器（本地规则版，无外部 LLM 依赖）。"""

    ALLOWED_CATEGORIES = {
        "momentum",
        "trend",
        "reversal",
        "volatility",
        "value",
        "quality",
        "growth",
        "size",
        "liquidity",
        "sentiment",
        "event",
        "risk_adjusted",
        "divergence",
        "custom",
    }

    def __init__(self):
        """初始化 Alpha 挖掘器。"""
        self.factor_candidates = []
        self.evaluation_results = {}

    @staticmethod
    def _extract_news_headlines(news_data: Optional[List[Dict]], limit: int = 20) -> List[str]:
        if not news_data:
            return []
        headlines = []
        for item in news_data:
            if not isinstance(item, dict):
                continue
            text = (
                item.get("title")
                or item.get("headline")
                or item.get("summary")
                or item.get("content")
                or ""
            )
            text = str(text).strip()
            if text:
                headlines.append(text[:160])
            if len(headlines) >= limit:
                break
        return headlines

    @staticmethod
    def _build_market_snapshot(market_data: pd.DataFrame) -> Dict[str, Any]:
        """构建紧凑市场快照，供本地规则生成因子。"""
        if market_data is None or market_data.empty:
            return {"rows": 0, "columns": [], "numeric_summary": {}}

        df = market_data.tail(min(len(market_data), 180)).copy()
        numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]

        summary = {
            "rows": int(len(df)),
            "columns": [str(c) for c in df.columns.tolist()],
            "numeric_summary": {},
        }
        for col in numeric_cols[:12]:
            series = df[col].dropna()
            if len(series) == 0:
                continue
            summary["numeric_summary"][str(col)] = {
                "latest": float(series.iloc[-1]),
                "mean": float(series.mean()),
                "std": float(series.std() if len(series) > 1 else 0.0),
                "min": float(series.min()),
                "max": float(series.max()),
            }

        if "close" in df.columns and pd.api.types.is_numeric_dtype(df["close"]):
            close = df["close"].dropna()
            if len(close) >= 2:
                summary["close_stats"] = {
                    "period_return": float(close.iloc[-1] / close.iloc[0] - 1.0),
                    "volatility": float(close.pct_change().dropna().std() if len(close) > 2 else 0.0),
                }

        return summary

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = "_".join(str(name or "").strip().split())
        return normalized[:64]

    @staticmethod
    def _find_col(df: pd.DataFrame, aliases: List[str]) -> Optional[str]:
        columns = [str(c) for c in df.columns]
        lower_map = {str(c).lower(): str(c) for c in columns}
        for alias in aliases:
            key = str(alias).lower()
            if key in lower_map:
                return lower_map[key]
        return None

    def _normalize_candidates(self, raw_candidates: List[Dict[str, Any]], num_candidates: int) -> List[Dict[str, Any]]:
        cleaned: List[Dict[str, Any]] = []
        used_names = set()

        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            name = self._normalize_name(item.get("name"))
            description = str(item.get("description") or "").strip()
            formula = str(item.get("formula") or "").strip()
            category = str(item.get("category") or "custom").strip().lower()
            rationale = str(item.get("rationale") or "").strip()

            if not name or not formula:
                continue
            if category not in self.ALLOWED_CATEGORIES:
                category = "custom"
            if name in used_names:
                suffix = 2
                new_name = f"{name}_{suffix}"
                while new_name in used_names:
                    suffix += 1
                    new_name = f"{name}_{suffix}"
                name = new_name
            used_names.add(name)

            cleaned.append(
                {
                    "name": name,
                    "description": description[:300] if description else f"Local generated factor: {name}",
                    "formula": " ".join(formula.split())[:500],
                    "category": category,
                    "rationale": rationale[:500] if rationale else "",
                    "_engine": item.get("_engine", ""),
                    "_text_signal": item.get("_text_signal"),
                }
            )
            if len(cleaned) >= num_candidates:
                break

        if not cleaned:
            raise ValueError("未生成可用候选因子")
        return cleaned

    def _build_local_candidate_pool(
        self,
        market_data: pd.DataFrame,
        news_data: Optional[List[Dict]] = None,
    ) -> List[Dict[str, Any]]:
        df = market_data
        close_col = self._find_col(df, ["close", "收盘", "close_price"])
        open_col = self._find_col(df, ["open", "开盘"])
        high_col = self._find_col(df, ["high", "最高"])
        low_col = self._find_col(df, ["low", "最低"])
        volume_col = self._find_col(df, ["volume", "vol", "成交量"])
        amount_col = self._find_col(df, ["amount", "turnover", "成交额"])
        market_cap_col = self._find_col(df, ["market_cap", "total_mv", "mkt_cap", "市值"])

        news_headlines = self._extract_news_headlines(news_data, limit=20)
        snapshot = self._build_market_snapshot(df)
        close_stats = snapshot.get("close_stats", {}) if isinstance(snapshot, dict) else {}
        period_return = float(close_stats.get("period_return", 0.0) or 0.0)
        volatility = float(close_stats.get("volatility", 0.0) or 0.0)

        pool: List[Dict[str, Any]] = []
        if close_col:
            pool.append({
                "name": "Momentum_20_60_Spread",
                "description": "中期与短期动量差，捕捉趋势加速。",
                "formula": f"({close_col}.pct_change(60) - {close_col}.pct_change(20))",
                "category": "momentum",
                "rationale": "趋势行情中更稳健地识别加速段。",
            })
            pool.append({
                "name": "Volatility_Adjusted_Return",
                "description": "收益按波动率归一，抑制高噪声区间。",
                "formula": f"({close_col}.pct_change(20)) / ({close_col}.pct_change().rolling(20).std() + 1e-9)",
                "category": "risk_adjusted",
                "rationale": "在波动放大时降低虚假强势信号。",
            })
            pool.append({
                "name": "Short_Term_Reversal_5D",
                "description": "短期反转因子，识别超跌/超涨回归。",
                "formula": f"-({close_col}.pct_change(5))",
                "category": "reversal",
                "rationale": "均值回复场景下提供反向信号。",
            })
            pool.append({
                "name": "Trend_ZScore_20",
                "description": "价格相对20日均线的标准分。",
                "formula": f"({close_col} - {close_col}.rolling(20).mean()) / ({close_col}.rolling(20).std() + 1e-9)",
                "category": "trend",
                "rationale": "衡量趋势偏离程度，便于横截面对比。",
            })

        if close_col and volume_col:
            pool.append({
                "name": "Price_Volume_Synergy",
                "description": "价格动量乘以成交量放大倍数。",
                "formula": f"{close_col}.pct_change(20) * ({volume_col} / ({volume_col}.rolling(20).mean() + 1e-9))",
                "category": "liquidity",
                "rationale": "量价共振通常对应更高可持续性。",
            })
            pool.append({
                "name": "Volume_Price_Divergence",
                "description": "量价背离，监测趋势衰减风险。",
                "formula": f"{close_col}.pct_change(5) - {volume_col}.pct_change(5)",
                "category": "divergence",
                "rationale": "价格上涨但量能收缩时给出预警。",
            })

        if high_col and low_col and close_col:
            pool.append({
                "name": "Intraday_Range_Pressure",
                "description": "日内振幅压力，衡量波动压缩/扩张。",
                "formula": f"(({high_col} - {low_col}) / ({close_col} + 1e-9)).rolling(10).mean()",
                "category": "volatility",
                "rationale": "振幅变化通常领先趋势转折。",
            })

        if open_col and close_col:
            pool.append({
                "name": "Gap_Continuation",
                "description": "隔夜跳空与日内收益一致性。",
                "formula": f"({open_col} / ({close_col}.shift(1) + 1e-9) - 1) * {close_col}.pct_change(1)",
                "category": "event",
                "rationale": "用于识别消息驱动后的延续性。",
            })

        if amount_col and market_cap_col:
            pool.append({
                "name": "Turnover_Pressure",
                "description": "成交额相对市值压力。",
                "formula": f"({amount_col} / ({market_cap_col} + 1e-9)).rolling(10).mean()",
                "category": "liquidity",
                "rationale": "高换手压力可反映交易拥挤度。",
            })

        if news_headlines and close_col and volume_col:
            # P2-1: 优先使用文本信号管线，失败时降级为量价代理
            try:
                text_signal = TextSignalPipeline.aggregate_signals(news_headlines)
                signal_score = text_signal.get("signal_score", 0.0)
                evidence = text_signal.get("evidence", [])
                evidence_str = ", ".join(e["keyword"] for e in evidence[:5]) if evidence else "无显著关键词"
                pool.append({
                    "name": "Text_Sentiment_Signal",
                    "description": f"文本信号管线情绪因子（{text_signal['sentiment']}），基于{text_signal['n_articles']}条新闻。",
                    "formula": f"text_signal_score={signal_score}; volume_confirm=({volume_col}/({volume_col}.rolling(5).mean()+1e-9)-1)*sign({signal_score})",
                    "category": "sentiment",
                    "rationale": f"文本信号得分 {signal_score}，关键词证据: {evidence_str}。",
                    "_engine": "text_signal_pipeline_v1",
                    "_text_signal": text_signal,
                })
            except Exception as exc:
                logger.debug("TextSignalPipeline failed, fallback to proxy: %s", exc)
                pool.append({
                    "name": "News_Attention_Proxy",
                    "description": "新闻关注度代理（量能突增 × 短期动量）— 文本管线降级。",
                    "formula": f"(({volume_col} / ({volume_col}.rolling(5).mean() + 1e-9)) - 1) * {close_col}.pct_change(3)",
                    "category": "sentiment",
                    "rationale": f"样本包含 {len(news_headlines)} 条近期新闻，文本管线不可用，使用交易行为代理。",
                    "_engine": "fallback_proxy",
                })

        # 真实 IC 重排:对自生成的因子公式算历史 IC,按 |IC| 降序优先高预测力因子。
        # 公式均为本函数自产模板(非外部输入),用受限命名空间 eval;任一失败诚实降级到启发式排序。
        pool = self._rank_pool_by_real_ic(
            pool,
            market_data=df,
            close_col=close_col,
            period_return=period_return,
            volatility=volatility,
        )

        return pool

    def _rank_pool_by_real_ic(
        self,
        pool: List[Dict[str, Any]],
        *,
        market_data: pd.DataFrame,
        close_col: Optional[str],
        period_return: float,
        volatility: float,
        forward_days: int = 5,
    ) -> List[Dict[str, Any]]:
        """用真实历史 IC 对本地因子池重排。

        每个因子公式在受限命名空间下求值为因子序列,与 forward_days 日前向收益对齐,
        调用已有 evaluate_factor 得到真实 IC。按 |IC| 降序排,取不到 IC 的因子(如文本信号
        公式无法单表达式求值)保持原相对序并排在已评分因子之后。整体失败时退回原启发式排序。
        """
        def _heuristic_sort(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            ordered = list(items)
            if abs(period_return) >= 0.1:
                ordered.sort(key=lambda x: 0 if x.get("category") in {"momentum", "trend"} else 1)
            if volatility >= 0.03:
                ordered.sort(key=lambda x: 0 if x.get("category") in {"volatility", "reversal", "risk_adjusted"} else 1)
            return ordered

        if not pool or close_col is None or market_data is None or market_data.empty:
            return _heuristic_sort(pool)

        try:
            namespace = {
                str(col): market_data[col]
                for col in market_data.columns
                if pd.api.types.is_numeric_dtype(market_data[col])
            }
            if close_col not in namespace:
                return _heuristic_sort(pool)
            forward_returns = market_data[close_col].pct_change(forward_days).shift(-forward_days)
            scored: List[tuple[float, int, Dict[str, Any]]] = []
            unscored: List[tuple[int, Dict[str, Any]]] = []
            for idx, candidate in enumerate(pool):
                formula = str(candidate.get("formula") or "")
                ic_abs = self._safe_formula_ic(formula, namespace, forward_returns)
                if ic_abs is None:
                    unscored.append((idx, candidate))
                else:
                    candidate["_real_ic_abs"] = round(float(ic_abs), 6)
                    scored.append((ic_abs, idx, candidate))
            if not scored:
                return _heuristic_sort(pool)
            # |IC| 降序;同分按原序稳定
            scored.sort(key=lambda t: (-t[0], t[1]))
            ranked = [c for _, _, c in scored] + [c for _, c in unscored]
            return ranked
        except Exception as exc:  # noqa: BLE001 - 评分失败不得拖垮生成,诚实退回启发式
            logger.debug("local factor IC ranking failed, fallback to heuristic: %s", exc)
            return _heuristic_sort(pool)

    def _safe_formula_ic(
        self,
        formula: str,
        namespace: Dict[str, Any],
        forward_returns: pd.Series,
    ) -> Optional[float]:
        """对单条因子公式求值并返回 |IC|;无法求值/样本不足时返回 None。"""
        formula = formula.strip()
        # 仅支持单一 pandas 表达式;含赋值/分号的(如文本信号公式)跳过
        if not formula or ";" in formula or "=" in formula.replace("==", "").replace(">=", "").replace("<=", "").replace("!=", ""):
            return None
        try:
            factor_series = eval(formula, {"__builtins__": {}}, dict(namespace))  # noqa: S307 - 公式为本模块自产模板
        except Exception:
            return None
        if not isinstance(factor_series, pd.Series):
            return None
        metrics = self.evaluate_factor(factor_series, forward_returns)
        ic = metrics.get("ic")
        if ic is None:
            return None
        try:
            ic_val = float(ic)
        except (TypeError, ValueError):
            return None
        if ic_val != ic_val:  # NaN
            return None
        return abs(ic_val)

    def generate_factor_candidates(
        self,
        market_data: pd.DataFrame,
        news_data: Optional[List[Dict]] = None,
        num_candidates: int = 10
    ) -> List[Dict[str, Any]]:
        """
        使用本地规则生成候选因子
        
        Args:
            market_data: 市场数据（包含价格、成交量等）
            news_data: 新闻数据（可选）
            num_candidates: 生成候选因子数量
            
        Returns:
            候选因子列表，每个因子包含：
            {
                'factor_id': 因子ID,
                'name': 因子名称,
                'description': 因子描述,
                'formula': 因子计算公式,
                'category': 因子类别（momentum/reversal/value等）
            }
        """
        if market_data is None or market_data.empty:
            raise ValueError("market_data 不能为空")

        num_candidates = max(1, min(int(num_candidates), 30))
        raw_pool = self._build_local_candidate_pool(market_data=market_data, news_data=news_data)
        normalized = self._normalize_candidates(raw_pool, num_candidates=num_candidates)

        timestamp = datetime.now().isoformat()
        candidates: List[Dict[str, Any]] = []
        for i, item in enumerate(normalized, 1):
            engine = item.get("_engine") or "local_rule_v1"
            candidate = {
                "factor_id": f"ALPHA_FACTOR_{int(datetime.now().timestamp())}_{i:03d}",
                "name": item["name"],
                "description": item["description"],
                "formula": item["formula"],
                "category": item["category"],
                "rationale": item.get("rationale", ""),
                "created_at": timestamp,
                "engine": engine,
            }
            # P2-1: 附加文本信号详情（可解释性）
            text_signal = item.get("_text_signal")
            if text_signal:
                candidate["text_signal"] = text_signal
            # B2: 透传真实 IC,供下游候选排序(B1)按预测力优先
            real_ic = item.get("_real_ic_abs")
            if real_ic is not None:
                candidate["real_ic_abs"] = real_ic
            candidates.append(candidate)

        self.factor_candidates.extend(candidates)
        return candidates
    
    def evaluate_factor(
        self,
        factor_values: pd.Series,
        returns: pd.Series,
        method: str = 'ic'
    ) -> Dict[str, float]:
        """
        评估因子有效性
        
        Args:
            factor_values: 因子值序列
            returns: 收益率序列
            method: 评估方法（ic/rank_ic/sharpe）
            
        Returns:
            {
                'ic': IC值,
                'rank_ic': Rank IC值,
                'ic_ir': IC信息比率,
                'positive_rate': IC为正的比例,
                't_stat': t统计量,
                'p_value': p值
            }
        """
        # 对齐数据
        aligned_data = pd.DataFrame({
            'factor': factor_values,
            'return': returns
        }).dropna()
        
        if len(aligned_data) < 10:
            return {
                'ic': 0.0,
                'rank_ic': 0.0,
                'ic_ir': 0.0,
                'positive_rate': 0.0,
                't_stat': 0.0,
                'p_value': 1.0
            }
        
        # 计算IC
        ic = aligned_data['factor'].corr(aligned_data['return'])
        
        # 计算Rank IC
        rank_ic = aligned_data['factor'].corr(aligned_data['return'], method='spearman')
        
        # 计算IC序列（滚动窗口）
        window = 20
        ic_series = []
        for i in range(window, len(aligned_data)):
            window_data = aligned_data.iloc[i-window:i]
            window_ic = window_data['factor'].corr(window_data['return'])
            ic_series.append(window_ic)
        
        ic_series = pd.Series(ic_series)
        
        # 计算IC IR
        ic_mean = ic_series.mean()
        ic_std = ic_series.std()
        ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0
        
        # 计算IC为正的比例
        positive_rate = (ic_series > 0).sum() / len(ic_series) if len(ic_series) > 0 else 0.0
        
        # 计算t统计量
        t_stat = ic_mean / (ic_std / np.sqrt(len(ic_series))) if ic_std > 0 else 0.0
        
        # 计算p值（简化版）
        from scipy import stats
        p_value = 2 * (1 - stats.t.cdf(abs(t_stat), len(ic_series) - 1))
        
        return {
            'ic': float(ic),
            'rank_ic': float(rank_ic),
            'ic_ir': float(ic_ir),
            'positive_rate': float(positive_rate),
            't_stat': float(t_stat),
            'p_value': float(p_value)
        }

    def detect_alpha_decay(
        self,
        factor_values: pd.Series,
        returns: pd.Series,
        window: int = 60
    ) -> Dict[str, Any]:
        """
        检测因子Alpha衰减

        Args:
            factor_values: 因子值序列
            returns: 收益率序列
            window: 滚动窗口大小

        Returns:
            {
                'is_decaying': 是否衰减,
                'decay_rate': 衰减率,
                'half_life': 半衰期（天数）,
                'recent_ic': 最近IC,
                'historical_ic': 历史IC
            }
        """
        # 对齐数据
        aligned_data = pd.DataFrame({
            'factor': factor_values,
            'return': returns
        }).dropna()

        if len(aligned_data) < window * 2:
            return {
                'is_decaying': False,
                'decay_rate': 0.0,
                'half_life': np.inf,
                'recent_ic': 0.0,
                'historical_ic': 0.0
            }

        # 计算滚动IC
        ic_series = []
        for i in range(window, len(aligned_data)):
            window_data = aligned_data.iloc[i-window:i]
            window_ic = window_data['factor'].corr(window_data['return'])
            ic_series.append(window_ic)

        ic_series = pd.Series(ic_series)

        # 计算最近和历史IC
        recent_ic = ic_series.iloc[-window//2:].mean()
        historical_ic = ic_series.iloc[:window//2].mean()

        # 计算衰减率
        if historical_ic != 0:
            decay_rate = (historical_ic - recent_ic) / abs(historical_ic)
        else:
            decay_rate = 0.0

        # 判断是否衰减（最近IC显著低于历史IC）
        is_decaying = decay_rate > 0.3  # 衰减超过30%

        # 计算半衰期（简化版：假设指数衰减）
        if decay_rate > 0:
            half_life = np.log(2) / np.log(1 + decay_rate) * window
        else:
            half_life = np.inf

        return {
            'is_decaying': bool(is_decaying),
            'decay_rate': float(decay_rate),
            'half_life': float(half_life),
            'recent_ic': float(recent_ic),
            'historical_ic': float(historical_ic)
        }

    def optimize_factor_combination(
        self,
        factors: Dict[str, pd.Series],
        returns: pd.Series,
        method: str = 'ic_weight'
    ) -> Dict[str, float]:
        """
        优化因子组合权重

        Args:
            factors: 因子字典 {factor_name: factor_values}
            returns: 收益率序列
            method: 优化方法（ic_weight/equal_weight/max_sharpe）

        Returns:
            因子权重字典 {factor_name: weight}
        """
        if method == 'equal_weight':
            # 等权重
            n = len(factors)
            return {name: 1.0/n for name in factors.keys()}

        elif method == 'ic_weight':
            # IC加权
            ic_values = {}
            for name, factor_values in factors.items():
                evaluation = self.evaluate_factor(factor_values, returns)
                ic_values[name] = abs(evaluation['ic'])

            # 归一化
            total_ic = sum(ic_values.values())
            if total_ic > 0:
                return {name: ic/total_ic for name, ic in ic_values.items()}
            else:
                n = len(factors)
                return {name: 1.0/n for name in factors.keys()}

        elif method == 'max_sharpe':
            # 最大化夏普比率（简化版）
            from scipy.optimize import minimize

            # 计算因子收益矩阵
            factor_returns = pd.DataFrame({
                name: factor_values * returns
                for name, factor_values in factors.items()
            }).dropna()

            if len(factor_returns) < 10:
                n = len(factors)
                return {name: 1.0/n for name in factors.keys()}

            # 计算协方差矩阵
            cov_matrix = factor_returns.cov()
            mean_returns = factor_returns.mean()

            # 优化目标：最大化夏普比率
            def neg_sharpe(weights):
                portfolio_return = np.dot(weights, mean_returns)
                portfolio_vol = np.sqrt(np.dot(weights, np.dot(cov_matrix, weights)))
                return -portfolio_return / portfolio_vol if portfolio_vol > 0 else 0

            # 约束条件
            n = len(factors)
            constraints = {'type': 'eq', 'fun': lambda x: np.sum(x) - 1}
            bounds = tuple((0, 1) for _ in range(n))
            initial_weights = np.array([1.0/n] * n)

            # 优化
            result = minimize(
                neg_sharpe,
                initial_weights,
                method='SLSQP',
                bounds=bounds,
                constraints=constraints
            )

            if result.success:
                return {name: float(weight) for name, weight in zip(factors.keys(), result.x)}
            else:
                n = len(factors)
                return {name: 1.0/n for name in factors.keys()}

        else:
            raise ValueError(f"Unknown method: {method}")

    def get_top_factors(
        self,
        factors: Dict[str, pd.Series],
        returns: pd.Series,
        top_n: int = 5
    ) -> List[Dict[str, Any]]:
        """
        获取表现最好的因子

        Args:
            factors: 因子字典
            returns: 收益率序列
            top_n: 返回前N个因子

        Returns:
            排序后的因子列表
        """
        factor_scores = []

        for name, factor_values in factors.items():
            evaluation = self.evaluate_factor(factor_values, returns)
            decay = self.detect_alpha_decay(factor_values, returns)

            # 综合评分：IC * IC_IR * (1 - decay_rate)
            score = abs(evaluation['ic']) * evaluation['ic_ir'] * (1 - decay['decay_rate'])

            factor_scores.append({
                'name': name,
                'score': float(score),
                'ic': evaluation['ic'],
                'ic_ir': evaluation['ic_ir'],
                'is_decaying': decay['is_decaying'],
                'decay_rate': decay['decay_rate']
            })

        # 按评分排序
        factor_scores.sort(key=lambda x: x['score'], reverse=True)

        return factor_scores[:top_n]


# 创建全局实例
llm_alpha_miner = LLMAlphaMiner()
