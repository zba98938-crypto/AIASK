"""DB-first loaders for market text/fund-flow context."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional


_DOC_TYPE_ALIASES = {
    "news": ("news", "stock_news", "headline"),
    "notice": ("notice", "notices", "announcement", "announcements", "stock_notice"),
    "research": ("research", "report", "reports", "research_report", "research_reports"),
}


def _coerce_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    for parser in (
        lambda item: date.fromisoformat(item[:10]),
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")).date(),
    ):
        try:
            return parser(text)
        except Exception:
            continue
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        try:
            return date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
        except Exception:
            return None
    return None


def _coerce_date_str(value: Any) -> str:
    parsed = _coerce_date(value)
    return parsed.isoformat() if parsed else ""


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _pick_first(payload: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _title_from_content(content: str, *, limit: int = 120) -> str:
    text = _clean_text(content).replace("\r", " ").replace("\n", " ")
    if not text:
        return ""
    return text[:limit]


def _map_vector_row(row: dict[str, Any], *, default_source: str) -> dict[str, Any]:
    content = _clean_text(row.get("content"))
    title = _title_from_content(content)
    date_str = _coerce_date_str(row.get("date"))
    return {
        "title": title,
        "headline": title,
        "content": content,
        "text": content,
        "summary": content[:240],
        "date": date_str,
        "time": date_str,
        "source": default_source,
        "url": "",
    }


def _map_market_chunk_row(row: dict[str, Any], *, default_source: str) -> dict[str, Any]:
    content = _clean_text(row.get("chunk_text") or row.get("content"))
    title = _clean_text(row.get("title")) or _title_from_content(content)
    date_str = _coerce_date_str(row.get("published_at") or row.get("date"))
    return {
        "title": title,
        "headline": title,
        "content": content,
        "text": content,
        "summary": _clean_text(row.get("summary")) or content[:240],
        "date": date_str,
        "time": date_str,
        "source": _clean_text(row.get("source")) or default_source,
        "url": _clean_text(row.get("url")),
    }


def _map_research_row(row: dict[str, Any], *, default_source: str = "research_reports") -> dict[str, Any]:
    date_str = _coerce_date_str(row.get("publish_date") or row.get("date"))
    summary = _clean_text(row.get("summary"))
    return {
        "title": _clean_text(row.get("title")),
        "institution": _clean_text(row.get("institution")),
        "author": _clean_text(row.get("analyst") or row.get("author")),
        "rating": _clean_text(row.get("rating")),
        "targetPrice": _coerce_float(row.get("target_price") or row.get("targetPrice")),
        "date": date_str,
        "time": date_str,
        "summary": summary,
        "content": summary,
        "text": summary,
        "url": _clean_text(row.get("pdf_url") or row.get("url")),
        "source": default_source,
    }


async def _fetch_vector_documents(
    conn,
    code: str,
    *,
    kind: str,
    limit: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[dict[str, Any]]:
    normalized_code = str(code or "").strip()
    if not normalized_code or int(limit or 0) <= 0:
        return []

    aliases = [str(item).strip().lower() for item in _DOC_TYPE_ALIASES.get(kind, ()) if str(item).strip()]
    if not aliases:
        return []

    params: list[Any] = [normalized_code, aliases]
    where_clauses = [
        "stock_code = $1",
        "LOWER(COALESCE(doc_type, '')) IN ($2)",
    ]

    if start_date is not None:
        params.append(start_date)
        where_clauses.append(f"date >= ${len(params)}")
    if end_date is not None:
        params.append(end_date)
        where_clauses.append(f"date <= ${len(params)}")

    params.append(int(limit))
    rows = await conn.fetch(
        f"""
        SELECT id, doc_type, content, date
        FROM vector_documents
        WHERE {' AND '.join(where_clauses)}
        ORDER BY date DESC NULLS LAST, id DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    default_source = f"vector_documents_legacy.{kind}"
    return [_map_vector_row(dict(row), default_source=default_source) for row in rows if dict(row).get("content")]


async def _fetch_market_doc_chunks(
    conn,
    code: str,
    *,
    kind: str,
    limit: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[dict[str, Any]]:
    normalized_code = str(code or "").strip()
    if not normalized_code or int(limit or 0) <= 0:
        return []

    aliases = [str(item).strip().lower() for item in _DOC_TYPE_ALIASES.get(kind, ()) if str(item).strip()]
    if not aliases:
        return []

    params: list[Any] = [normalized_code, aliases]
    where_clauses = [
        "c.stock_code = $1",
        "LOWER(COALESCE(c.doc_type, '')) IN ($2)",
    ]
    if start_date is not None:
        params.append(start_date)
        where_clauses.append(f"c.published_at >= ${len(params)}")
    if end_date is not None:
        params.append(end_date)
        where_clauses.append(f"c.published_at <= ${len(params)}")

    params.append(int(limit))
    rows = await conn.fetch(
        f"""
        SELECT
            c.id,
            c.title,
            c.chunk_text,
            c.published_at,
            c.source,
            d.summary,
            d.url
        FROM market_doc_chunks c
        JOIN market_documents d ON d.id = c.doc_id
        WHERE {' AND '.join(where_clauses)}
        ORDER BY c.published_at DESC NULLS LAST, c.id DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    default_source = f"market_doc_chunks.{kind}"
    return [_map_market_chunk_row(dict(row), default_source=default_source) for row in rows if dict(row).get("chunk_text")]


async def _fetch_research_reports(
    conn,
    code: str,
    *,
    limit: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[dict[str, Any]]:
    normalized_code = str(code or "").strip()
    if not normalized_code or int(limit or 0) <= 0:
        return []

    params: list[Any] = [normalized_code]
    where_clauses = ["code = $1"]
    if start_date is not None:
        params.append(start_date)
        where_clauses.append(f"publish_date >= ${len(params)}")
    if end_date is not None:
        params.append(end_date)
        where_clauses.append(f"publish_date <= ${len(params)}")

    params.append(int(limit))
    rows = await conn.fetch(
        f"""
        SELECT code, title, rating, target_price, institution, analyst, publish_date, summary, pdf_url
        FROM research_reports
        WHERE {' AND '.join(where_clauses)}
        ORDER BY publish_date DESC NULLS LAST, id DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return [_map_research_row(dict(row)) for row in rows if dict(row).get("title") or dict(row).get("summary")]


async def load_db_first_document_context(
    db,
    code: str,
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    news_limit: int = 0,
    notice_limit: int = 0,
    research_limit: int = 0,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Load news/notices/research context from DB before falling back to external tools."""

    context: dict[str, list[dict[str, Any]]] = {
        "news": [],
        "notices": [],
        "research": [],
    }
    source_chain: list[str] = []
    if not callable(getattr(db, "acquire", None)):
        return context, source_chain

    try:
        async with db.acquire() as conn:
            if int(news_limit or 0) > 0:
                try:
                    context["news"] = await _fetch_market_doc_chunks(
                        conn,
                        code,
                        kind="news",
                        limit=int(news_limit),
                        start_date=start_date,
                        end_date=end_date,
                    )
                except Exception:
                    context["news"] = []
                if context["news"]:
                    source_chain.append("db.market_doc_chunks.news")
                else:
                    try:
                        context["news"] = await _fetch_vector_documents(
                            conn,
                            code,
                            kind="news",
                            limit=int(news_limit),
                            start_date=start_date,
                            end_date=end_date,
                        )
                    except Exception:
                        context["news"] = []
                    if context["news"]:
                        source_chain.append("db.vector_documents_legacy.news")

            if int(notice_limit or 0) > 0:
                try:
                    context["notices"] = await _fetch_market_doc_chunks(
                        conn,
                        code,
                        kind="notice",
                        limit=int(notice_limit),
                        start_date=start_date,
                        end_date=end_date,
                    )
                except Exception:
                    context["notices"] = []
                if context["notices"]:
                    source_chain.append("db.market_doc_chunks.notice")
                else:
                    try:
                        context["notices"] = await _fetch_vector_documents(
                            conn,
                            code,
                            kind="notice",
                            limit=int(notice_limit),
                            start_date=start_date,
                            end_date=end_date,
                        )
                    except Exception:
                        context["notices"] = []
                    if context["notices"]:
                        source_chain.append("db.vector_documents_legacy.notice")

            if int(research_limit or 0) > 0:
                try:
                    context["research"] = await _fetch_research_reports(
                        conn,
                        code,
                        limit=int(research_limit),
                        start_date=start_date,
                        end_date=end_date,
                    )
                except Exception:
                    context["research"] = []
                if context["research"]:
                    source_chain.append("db.research_reports")
                else:
                    try:
                        context["research"] = await _fetch_market_doc_chunks(
                            conn,
                            code,
                            kind="research",
                            limit=int(research_limit),
                            start_date=start_date,
                            end_date=end_date,
                        )
                    except Exception:
                        context["research"] = []
                    if context["research"]:
                        source_chain.append("db.market_doc_chunks.research")
                    else:
                        try:
                            context["research"] = await _fetch_vector_documents(
                                conn,
                                code,
                                kind="research",
                                limit=int(research_limit),
                                start_date=start_date,
                                end_date=end_date,
                            )
                        except Exception:
                            context["research"] = []
                        if context["research"]:
                            source_chain.append("db.vector_documents_legacy.research")
    except Exception:
        return context, source_chain

    return context, source_chain


async def load_db_first_stock_fund_flow(
    db,
    code: str,
) -> tuple[dict[str, Any], list[str]]:
    """Load latest stock fund-flow snapshot from DB when available."""

    normalized_code = str(code or "").strip()
    if not normalized_code or not callable(getattr(db, "acquire", None)):
        return {}, []

    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM stock_fund_flow
                WHERE code = $1
                ORDER BY trade_date DESC NULLS LAST, updated_at DESC NULLS LAST
                LIMIT 1
                """,
                normalized_code,
            )
    except Exception:
        return {}, []

    if not row:
        return {}, []

    payload = dict(row)
    result = {
        "code": _clean_text(_pick_first(payload, ["code"])) or normalized_code,
        "name": _clean_text(_pick_first(payload, ["name", "stock_name"])),
        "mainNetInflow": _coerce_float(_pick_first(payload, ["mainNetInflow", "main_net_inflow", "main_inflow", "net_inflow"])),
        "mainInflowPercent": _coerce_float(_pick_first(payload, ["mainInflowPercent", "main_inflow_percent"])),
        "superLargeNetInflow": _coerce_float(_pick_first(payload, ["superLargeNetInflow", "super_large_net_inflow"])),
        "largeNetInflow": _coerce_float(_pick_first(payload, ["largeNetInflow", "large_net_inflow"])),
        "middleNetInflow": _coerce_float(_pick_first(payload, ["middleNetInflow", "middle_net_inflow"])),
        "smallNetInflow": _coerce_float(_pick_first(payload, ["smallNetInflow", "small_net_inflow", "retail_net_inflow"])),
        "tradeDate": _coerce_date_str(_pick_first(payload, ["trade_date"])),
        "source": _clean_text(_pick_first(payload, ["source"])) or "stock_fund_flow",
    }
    return result, ["db.stock_fund_flow"]


__all__ = [
    "load_db_first_document_context",
    "load_db_first_stock_fund_flow",
]
