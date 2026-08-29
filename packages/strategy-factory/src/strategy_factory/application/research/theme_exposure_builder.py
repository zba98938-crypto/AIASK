"""TDX-only theme exposure matrix builder.

Phase 6 v1 computes exposure from local SQLite TDX/cache tables only:
industry relation, stock/name aliases, concept blocks, and liquidity/market
cap filters. It intentionally has no Tushare, AKShare, eFinance, Baostock, or
main-business fallback.
"""

from __future__ import annotations

import json
import logging
import math
import os
from time import perf_counter
from typing import Any

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "--"):
            return default
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _normalize_text(text: Any) -> str:
    return str(text or "").strip().lower()


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            loaded = json.loads(stripped)
        except Exception:
            loaded = [stripped]
    elif isinstance(value, (list, tuple, set)):
        loaded = list(value)
    else:
        loaded = [value]
    result: list[str] = []
    for item in loaded:
        if isinstance(item, dict):
            item = item.get("theme_code") or item.get("name") or item.get("theme_name")
        token = str(item or "").strip()
        if token:
            result.append(token)
    return result


def _keyword_match_score(text: str, keywords: list[str]) -> float:
    """Score keyword hits in text. Returns 0-1."""

    normalized = _normalize_text(text)
    terms = [_normalize_text(item) for item in keywords if _normalize_text(item)]
    if not normalized or not terms:
        return 0.0
    matched = 0
    for term in terms:
        if term and (term in normalized or normalized in term):
            matched += 1
    return min(1.0, matched / max(len(terms), 1))


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _member_dilution(member_count: Any) -> float:
    count = int(_safe_float(member_count, 0.0))
    if count <= 0:
        return 1.0
    return _clamp(math.log10(150.0 / max(1, count)), 0.0, 1.0)


def _is_bad_list_status(value: Any) -> bool:
    token = _normalize_text(value)
    if not token:
        return False
    good = {"l", "listed", "active", "normal", "上市"}
    bad = {"d", "delisted", "退市", "suspend", "suspended", "暂停上市", "终止上市"}
    return token in bad or (token not in good and ("退" in token or "停" in token))


def _is_suspended_flag(value: Any) -> bool:
    token = _normalize_text(value)
    if not token:
        return False
    good = {"0", "false", "none", "null", "nan", "normal", "正常", "--"}
    return token not in good and ("停" in token or "suspend" in token or token in {"1", "true"})


class ThemeExposureBuilder:
    """Build and persist Phase 6 v1 TDX-only theme exposure rows."""

    W_INDUSTRY = 0.40
    W_NAME = 0.20
    W_CONCEPT = 0.30
    W_LIQUIDITY = 0.10

    def __init__(
        self,
        *,
        min_exposure: float | None = None,
        stock_limit: int | None = None,
        theme_limit: int | None = None,
        batch_size: int = 1000,
    ) -> None:
        self.min_exposure = (
            float(min_exposure)
            if min_exposure is not None
            else float(os.getenv("STRATEGY_FACTORY_THEME_EXPOSURE_MIN_SCORE", "0.1") or 0.1)
        )
        self.stock_limit = int(stock_limit or os.getenv("STRATEGY_FACTORY_THEME_EXPOSURE_STOCK_LIMIT", "10000") or 10000)
        self.theme_limit = int(theme_limit or os.getenv("STRATEGY_FACTORY_THEME_EXPOSURE_THEME_LIMIT", "300") or 300)
        self.batch_size = max(1, int(batch_size or 1000))

    async def build(self, db: Any, *, batch_size: int | None = None) -> dict[str, Any]:
        """Compute exposure rows and persist them using the bulk DAO."""

        start = perf_counter()
        theme_nodes = await self._load_theme_nodes(db)
        if not theme_nodes:
            return {"status": "skipped", "reason": "no_active_theme_nodes"}

        stocks = await self._load_stocks(db)
        if not stocks:
            return {"status": "skipped", "reason": "no_stocks_in_universe"}

        symbols = [str(item.get("code") or item.get("symbol") or "").strip() for item in stocks]
        symbols = [item for item in symbols if item]

        industry_rows = await self._safe_call(
            db,
            "list_industry_blocks",
            symbols=symbols,
            limit=max(len(symbols) * 4, 1000),
        )
        concept_rows = await self._safe_call(
            db,
            "list_company_concept_blocks",
            symbols=symbols,
            limit=max(len(symbols) * 20, 1000),
        )

        industry_by_symbol = self._group_by_symbol(industry_rows)
        concept_by_symbol = self._group_by_symbol(concept_rows)
        enriched_stocks = self._merge_stock_metadata(stocks, industry_by_symbol, concept_by_symbol)

        rows: list[dict[str, Any]] = []
        rows_scanned = 0
        skipped_low_liquidity = 0
        industry_matched_symbols: set[str] = set()
        concept_matched_symbols: set[str] = set()

        for theme in theme_nodes:
            theme_code = str(theme.get("theme_code") or "").strip()
            if not theme_code:
                continue
            theme_name = str(theme.get("theme_name") or theme_code).strip()
            aliases = _json_list(theme.get("aliases"))
            industry_tags = _json_list(theme.get("industry_tags"))
            keywords = self._theme_keywords(theme_code, theme_name, aliases, industry_tags)

            for stock in enriched_stocks:
                rows_scanned += 1
                symbol = str(stock.get("code") or stock.get("symbol") or "").strip()
                if not symbol:
                    continue
                liquidity = self._liquidity_score(stock)
                if liquidity["skip"]:
                    skipped_low_liquidity += 1
                    continue

                industry = self._industry_match(
                    stock,
                    industry_by_symbol.get(symbol, []),
                    industry_tags,
                    keywords,
                )
                name_score = _keyword_match_score(
                    " ".join([
                        str(stock.get("name") or ""),
                        str(stock.get("stock_name") or ""),
                    ]),
                    [theme_name, *aliases, theme_code],
                )
                concept = self._concept_match(
                    concept_by_symbol.get(symbol, []),
                    theme_code,
                    keywords,
                )
                if industry["score"] > 0:
                    industry_matched_symbols.add(symbol)
                if concept["score"] > 0:
                    concept_matched_symbols.add(symbol)

                theme_signal = industry["score"] + name_score + concept["score"]
                if theme_signal <= 0:
                    continue

                total = (
                    self.W_INDUSTRY * industry["score"]
                    + self.W_NAME * name_score
                    + self.W_CONCEPT * concept["score"]
                    + self.W_LIQUIDITY * liquidity["score"]
                )
                total = round(_clamp(total), 4)
                if total < self.min_exposure:
                    continue

                rows.append({
                    "symbol": symbol,
                    "theme_code": theme_code,
                    "exposure_score": total,
                    "industry_match_level": industry["level"],
                    "name_match_score": round(name_score, 4),
                    "mainbz_match_score": 0.0,
                    "historical_beta": 0.0,
                    "evidence": {
                        "source": "tdx_only_v1",
                        "formula": {
                            "industry": self.W_INDUSTRY,
                            "name_alias": self.W_NAME,
                            "concept_block": self.W_CONCEPT,
                            "liquidity_market_cap": self.W_LIQUIDITY,
                        },
                        "industry_score": round(industry["score"], 4),
                        "industry_match_level": industry["level"],
                        "matched_industries": industry["matched"],
                        "name_match_score": round(name_score, 4),
                        "concept_block_score": round(concept["score"], 4),
                        "matched_concepts": concept["matched"],
                        "liquidity_score": round(liquidity["score"], 4),
                        "liquidity_evidence": liquidity["evidence"],
                    },
                })

        if hasattr(db, "bulk_upsert_theme_exposure"):
            write_result = await db.bulk_upsert_theme_exposure(
                rows,
                batch_size=max(1, int(batch_size or self.batch_size)),
            )
        else:
            write_result = {"written": 0, "batch_count": 0, "skipped": len(rows)}

        elapsed = perf_counter() - start
        stock_count = len(enriched_stocks)
        return {
            "status": "completed",
            "source": "tdx_only_v1",
            "theme_count": len(theme_nodes),
            "stock_count": stock_count,
            "rows_scanned": rows_scanned,
            "rows_written": int(write_result.get("written") or 0),
            "batch_count": int(write_result.get("batch_count") or 0),
            "bulk_skipped": int(write_result.get("skipped") or 0),
            "elapsed_seconds": round(elapsed, 2),
            "industry_coverage": round(len(industry_matched_symbols) / max(stock_count, 1), 4),
            "concept_block_coverage": round(len(concept_matched_symbols) / max(stock_count, 1), 4),
            "skipped_low_liquidity": skipped_low_liquidity,
        }

    async def _load_theme_nodes(self, db: Any) -> list[dict[str, Any]]:
        if not hasattr(db, "list_theme_nodes"):
            return []
        rows = await db.list_theme_nodes(is_active=True, limit=max(1, self.theme_limit))
        return [dict(item or {}) for item in rows or []]

    async def _load_stocks(self, db: Any) -> list[dict[str, Any]]:
        if not hasattr(db, "list_stock_universe"):
            return []
        rows = await db.list_stock_universe(limit=max(1, self.stock_limit), offset=0)
        return [dict(item or {}) for item in rows or []]

    async def _safe_call(self, db: Any, method_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        method = getattr(db, method_name, None)
        if not callable(method):
            return []
        try:
            rows = await method(**kwargs)
        except Exception as exc:
            logger.debug("ThemeExposureBuilder: %s failed: %s", method_name, exc)
            return []
        return [dict(item or {}) for item in rows or []]

    @staticmethod
    def _group_by_symbol(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows or []:
            symbol = str(row.get("symbol") or row.get("code") or row.get("stock_code") or "").strip()
            if symbol:
                result.setdefault(symbol, []).append(row)
        return result

    @staticmethod
    def _merge_stock_metadata(
        stocks: list[dict[str, Any]],
        industry_by_symbol: dict[str, list[dict[str, Any]]],
        concept_by_symbol: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for raw in stocks:
            stock = dict(raw or {})
            symbol = str(stock.get("code") or stock.get("symbol") or "").strip()
            for source in list(industry_by_symbol.get(symbol, [])) + list(concept_by_symbol.get(symbol, [])):
                for key in (
                    "stock_name",
                    "tdx_industry",
                    "list_status",
                    "turnover_rate",
                    "zsz",
                    "ltsz",
                    "tp_flag",
                    "trade_date",
                ):
                    if stock.get(key) in (None, "") and source.get(key) not in (None, ""):
                        stock[key] = source.get(key)
                if stock.get("name") in (None, "") and source.get("stock_name"):
                    stock["name"] = source.get("stock_name")
            merged.append(stock)
        return merged

    @staticmethod
    def _theme_keywords(
        theme_code: str,
        theme_name: str,
        aliases: list[str],
        industry_tags: list[str],
    ) -> list[str]:
        ordered = [theme_code, theme_name, *aliases, *industry_tags]
        seen: set[str] = set()
        result: list[str] = []
        for item in ordered:
            token = _normalize_text(item)
            if not token or token in seen:
                continue
            seen.add(token)
            result.append(token)
        return result

    @staticmethod
    def _industry_match(
        stock: dict[str, Any],
        industry_rows: list[dict[str, Any]],
        industry_tags: list[str],
        keywords: list[str],
    ) -> dict[str, Any]:
        values = [
            stock.get("tdx_industry"),
            stock.get("industry"),
            stock.get("sector"),
            *[row.get("industry_name") or row.get("block_name") for row in industry_rows or []],
        ]
        texts = [_normalize_text(item) for item in values if _normalize_text(item)]
        tag_terms = [_normalize_text(item) for item in industry_tags if _normalize_text(item)]
        matched: list[str] = []
        level = 0
        for text in texts:
            if any(tag and (tag in text or text in tag) for tag in tag_terms):
                level = 2
                matched.append(text)
                break
        if level == 0:
            for text in texts:
                if any(len(kw) >= 2 and (kw in text or text in kw) for kw in keywords):
                    level = 1
                    matched.append(text)
                    break
        return {
            "level": level,
            "score": {0: 0.0, 1: 0.5, 2: 1.0}[level],
            "matched": matched[:5],
        }

    @staticmethod
    def _concept_match(
        concept_rows: list[dict[str, Any]],
        theme_code: str,
        keywords: list[str],
    ) -> dict[str, Any]:
        best = 0.0
        matched: list[dict[str, Any]] = []
        terms = list(keywords)
        theme_token = _normalize_text(theme_code)
        for row in concept_rows or []:
            block_name = str(row.get("block_name") or "")
            block_code = str(row.get("block_code") or "")
            text = " ".join([block_name, block_code])
            raw_score = _keyword_match_score(text, terms)
            normalized_text = _normalize_text(text)
            if theme_token and (theme_token in normalized_text or normalized_text in theme_token):
                raw_score = max(raw_score, 1.0)
            if raw_score <= 0:
                continue
            dilution = _member_dilution(row.get("member_count"))
            score = round(raw_score * dilution, 4)
            if score > best:
                best = score
            matched.append({
                "block_code": block_code,
                "block_name": block_name,
                "raw_score": round(raw_score, 4),
                "member_count": int(_safe_float(row.get("member_count"), 0)),
                "dilution": round(dilution, 4),
                "score": score,
            })
        matched.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
        return {"score": _clamp(best), "matched": matched[:5]}

    @staticmethod
    def _liquidity_score(stock: dict[str, Any]) -> dict[str, Any]:
        evidence = {
            "list_status": stock.get("list_status"),
            "tp_flag": stock.get("tp_flag"),
            "turnover_rate": stock.get("turnover_rate"),
            "market_cap": stock.get("market_cap"),
            "zsz": stock.get("zsz"),
            "ltsz": stock.get("ltsz"),
        }
        if _is_bad_list_status(stock.get("list_status")) or _is_suspended_flag(stock.get("tp_flag")):
            return {"skip": True, "score": 0.0, "evidence": evidence}

        turnover = _safe_float(stock.get("turnover_rate"), -1.0)
        market_cap = max(
            _safe_float(stock.get("market_cap"), 0.0),
            _safe_float(stock.get("zsz"), 0.0),
            _safe_float(stock.get("ltsz"), 0.0),
        )
        min_turnover = float(os.getenv("STRATEGY_FACTORY_THEME_EXPOSURE_MIN_TURNOVER", "0") or 0)
        min_market_cap = float(os.getenv("STRATEGY_FACTORY_THEME_EXPOSURE_MIN_MARKET_CAP", "0") or 0)
        if min_turnover > 0 and turnover >= 0 and turnover < min_turnover:
            return {"skip": True, "score": 0.0, "evidence": evidence}
        if min_market_cap > 0 and market_cap > 0 and market_cap < min_market_cap:
            return {"skip": True, "score": 0.0, "evidence": evidence}
        if turnover > 0 or market_cap > 0:
            return {"skip": False, "score": 1.0, "evidence": evidence}
        return {"skip": False, "score": 0.5, "evidence": evidence}


__all__ = ["ThemeExposureBuilder", "_keyword_match_score"]
