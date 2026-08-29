"""Stock radar ingest layer: RSS + PDF + document persistence."""

from __future__ import annotations

import hashlib
import os
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .analysis import PDF_TEXT_LIMIT, _as_bool, _clean, _positive_int

def _safe_feed_name(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_") or "rss"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{host}_{digest}"


def _rss_text(element: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        child = element.find(name)
        if child is not None and child.text:
            return _clean(child.text, 4000)
    return ""


def fetch_rss_feed_documents(feed_url: str, *, limit: int = 50, timeout: float = 12.0) -> list[dict[str, Any]]:
    response = requests.get(feed_url, timeout=timeout, headers={"User-Agent": "AIASK-StockRadar/1.0"})
    response.raise_for_status()
    root = ET.fromstring(response.content)
    rows = root.findall(".//item")
    if not rows:
        rows = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    docs: list[dict[str, Any]] = []
    feed_name = _safe_feed_name(feed_url)
    for item in rows[: max(0, min(int(limit or 0), 200))]:
        title = _rss_text(item, ("title", "{http://www.w3.org/2005/Atom}title"))
        summary = _rss_text(
            item,
            ("description", "summary", "content", "{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content"),
        )
        link = _rss_text(item, ("link", "{http://www.w3.org/2005/Atom}link"))
        atom_link = item.find("{http://www.w3.org/2005/Atom}link")
        if atom_link is not None:
            link = _clean(atom_link.attrib.get("href") or link, 1000)
        published_at = _rss_text(item, ("pubDate", "published", "updated", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"))
        if not title:
            continue
        original = link or f"{feed_url}:{title}:{published_at}"
        docs.append(
            {
                "doc_uid": f"rss:{feed_name}:{hashlib.sha1(original.encode('utf-8')).hexdigest()[:18]}",
                "title": title,
                "summary": summary or title,
                "content": " ".join(part for part in (title, summary[:800]) if part),
                "published_at": published_at,
                "date": published_at,
                "source": "rsshub",
                "source_tier": "tier_c",
                "provider": feed_name,
                "original_id": original[:240],
                "url": link,
                "reliability_score": 0.42,
                "metadata": {"feed_url": feed_url, "copyright_storage": "summary_only"},
            }
        )
    return docs


def _configured_rss_feeds() -> list[str]:
    raw = str(os.getenv("AIASK_RADAR_RSS_FEEDS") or "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.replace("\n", ",").replace(";", ",").split(",") if item.strip()]


def _pdf_url_from_doc(doc: dict[str, Any]) -> str:
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    for key in ("pdf_url", "url"):
        token = _clean(doc.get(key), 1000)
        if token:
            return token
    for key in ("pdf_url", "source_url", "url"):
        token = _clean(metadata.get(key), 1000)
        if token:
            return token
    return ""


def _looks_like_pdf_url(url: str) -> bool:
    if not url:
        return False
    path = (urlparse(url).path or url).lower()
    return path.endswith(".pdf") or ".pdf" in path


def _pdf_cache_dir() -> Path:
    raw = str(os.getenv("AIASK_RADAR_PDF_CACHE_DIR") or "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".aiask" / "stock-radar" / "pdfs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _download_pdf_file(url: str, *, timeout: float = 20.0) -> dict[str, Any]:
    max_bytes = _positive_int(os.getenv("AIASK_RADAR_PDF_MAX_BYTES"), 30 * 1024 * 1024, minimum=1024, maximum=120 * 1024 * 1024)
    cache_hint = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "AIASK-StockRadar/1.0"})
    response.raise_for_status()
    content = response.content or b""
    if len(content) > max_bytes:
        raise ValueError(f"pdf exceeds max bytes: {len(content)} > {max_bytes}")
    checksum = hashlib.sha256(content).hexdigest()
    path = _pdf_cache_dir() / f"{checksum[:32] or cache_hint}.pdf"
    if not path.exists():
        path.write_bytes(content)
    return {"local_pdf_path": str(path), "checksum": checksum, "bytes": len(content)}


def _parse_pdf_with_pymupdf(path: Path) -> dict[str, Any]:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        return {"parser": "pymupdf", "status": "unavailable", "error": f"{type(exc).__name__}: {exc}", "text": "", "pages": 0}
    try:
        parts: list[str] = []
        with fitz.open(str(path)) as pdf:
            pages = int(getattr(pdf, "page_count", 0) or len(pdf))
            for page in pdf:
                parts.append(str(page.get_text("text") or ""))
                if sum(len(part) for part in parts) >= PDF_TEXT_LIMIT:
                    break
        text = _clean(" ".join(parts), PDF_TEXT_LIMIT)
        return {"parser": "pymupdf", "status": "ok" if text else "empty", "text": text, "pages": pages}
    except Exception as exc:
        return {"parser": "pymupdf", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "text": "", "pages": 0}


def _parse_pdf_with_pdfplumber(path: Path) -> dict[str, Any]:
    try:
        import pdfplumber  # type: ignore
    except Exception as exc:
        return {"parser": "pdfplumber", "status": "unavailable", "error": f"{type(exc).__name__}: {exc}", "text": "", "pages": 0}
    try:
        parts: list[str] = []
        with pdfplumber.open(str(path)) as pdf:
            pages = len(pdf.pages)
            for page in pdf.pages:
                parts.append(str(page.extract_text() or ""))
                if sum(len(part) for part in parts) >= PDF_TEXT_LIMIT:
                    break
        text = _clean(" ".join(parts), PDF_TEXT_LIMIT)
        return {"parser": "pdfplumber", "status": "ok" if text else "empty", "text": text, "pages": pages}
    except Exception as exc:
        return {"parser": "pdfplumber", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "text": "", "pages": 0}


def _parse_pdf_with_paddleocr(path: Path) -> dict[str, Any]:
    if not _as_bool(os.getenv("AIASK_RADAR_ENABLE_OCR"), False):
        return {"parser": "paddleocr", "status": "disabled", "reason": "ocr_disabled", "text": "", "pages": 0}
    try:
        import fitz  # type: ignore
        from paddleocr import PaddleOCR  # type: ignore
    except Exception as exc:
        return {"parser": "paddleocr", "status": "unavailable", "error": f"{type(exc).__name__}: {exc}", "text": "", "pages": 0}
    max_pages = _positive_int(os.getenv("AIASK_RADAR_OCR_MAX_PAGES"), 3, minimum=1, maximum=12)
    try:
        ocr = PaddleOCR(use_angle_cls=True, lang="ch")
        parts: list[str] = []
        with tempfile.TemporaryDirectory(prefix="aiask_radar_ocr_") as tmpdir:
            with fitz.open(str(path)) as pdf:
                pages = min(int(getattr(pdf, "page_count", 0) or len(pdf)), max_pages)
                for page_idx in range(pages):
                    page = pdf.load_page(page_idx)
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                    img_path = Path(tmpdir) / f"page_{page_idx}.png"
                    pix.save(str(img_path))
                    result = ocr.ocr(str(img_path), cls=True)
                    for block in result or []:
                        for line in block or []:
                            if isinstance(line, (list, tuple)) and len(line) >= 2:
                                text_part = line[1][0] if isinstance(line[1], (list, tuple)) and line[1] else ""
                                if text_part:
                                    parts.append(str(text_part))
                    if sum(len(part) for part in parts) >= PDF_TEXT_LIMIT:
                        break
        text = _clean(" ".join(parts), PDF_TEXT_LIMIT)
        return {"parser": "paddleocr", "status": "ok" if text else "empty", "text": text, "pages": max_pages, "ocr_used": True}
    except Exception as exc:
        return {"parser": "paddleocr", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "text": "", "pages": 0}


def _extract_pdf_text_from_file(path: str | Path) -> dict[str, Any]:
    pdf_path = Path(path)
    attempts: list[dict[str, Any]] = []
    best_text = ""
    best_parser = ""
    best_pages = 0
    for parser in (_parse_pdf_with_pymupdf, _parse_pdf_with_pdfplumber):
        result = parser(pdf_path)
        attempts.append({key: value for key, value in result.items() if key != "text"})
        text = _clean(result.get("text"), PDF_TEXT_LIMIT)
        if len(text) > len(best_text):
            best_text = text
            best_parser = str(result.get("parser") or "")
            best_pages = int(result.get("pages") or best_pages or 0)
        if text and (len(text) / max(int(result.get("pages") or 1), 1)) >= 80:
            break
    density = round(len(best_text) / max(best_pages or 1, 1), 4)
    if best_text and density >= 80:
        return {
            "status": "ok",
            "parser": best_parser,
            "text": best_text,
            "pages": best_pages,
            "text_density": density,
            "parser_attempts": attempts,
        }
    ocr_result = _parse_pdf_with_paddleocr(pdf_path)
    attempts.append({key: value for key, value in ocr_result.items() if key != "text"})
    ocr_text = _clean(ocr_result.get("text"), PDF_TEXT_LIMIT)
    if len(ocr_text) > len(best_text):
        best_text = ocr_text
        best_parser = str(ocr_result.get("parser") or best_parser)
        best_pages = int(ocr_result.get("pages") or best_pages or 0)
        density = round(len(best_text) / max(best_pages or 1, 1), 4)
    if best_text:
        status = "ok" if str(ocr_result.get("status")) == "ok" else "degraded"
        return {
            "status": status,
            "reason": "low_text_density" if status == "degraded" else None,
            "parser": best_parser,
            "text": best_text,
            "pages": best_pages,
            "text_density": density,
            "ocr_status": ocr_result.get("status"),
            "parser_attempts": attempts,
        }
    unavailable = [item for item in attempts if item.get("status") == "unavailable"]
    return {
        "status": "degraded",
        "reason": "pdf_parser_unavailable" if len(unavailable) >= 2 else "pdf_text_empty",
        "parser": best_parser or "none",
        "text": "",
        "pages": best_pages,
        "text_density": 0.0,
        "ocr_status": ocr_result.get("status"),
        "parser_attempts": attempts,
    }


def _pdf_parse_status(doc: dict[str, Any], *, parse_pdf: bool, allow_network: bool) -> dict[str, Any]:
    url = _pdf_url_from_doc(doc)
    if not _looks_like_pdf_url(url):
        return {"status": "not_pdf"}
    if not parse_pdf:
        return {"status": "disabled", "url": url}
    if not allow_network:
        return {"status": "degraded", "reason": "network_disabled", "url": url}
    try:
        from . import _download_pdf_file as _dl, _extract_pdf_text_from_file as _xt

        downloaded = _dl(url)
        parsed = _xt(downloaded["local_pdf_path"])
        return {
            **parsed,
            "url": url,
            "local_pdf_path": downloaded["local_pdf_path"],
            "checksum": downloaded["checksum"],
            "bytes": downloaded["bytes"],
            "parser_order": ["pymupdf", "pdfplumber", "paddleocr"],
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "reason": "pdf_download_or_parse_failed",
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
            "parser_order": ["pymupdf", "pdfplumber", "paddleocr"],
        }


def _pdf_metadata_for_persist(pdf_status: dict[str, Any], checksum: Any = None) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            **pdf_status,
            "checksum": pdf_status.get("checksum") or checksum,
        }.items()
        if key not in {"text"} and value is not None
    }


async def _merge_document_metadata(db, doc_uid: str, metadata: dict[str, Any]) -> bool:
    handler = getattr(db, "merge_market_document_metadata", None)
    if not callable(handler):
        return False
    updated = await handler(doc_uid, metadata)
    return bool(updated)


async def _list_recent_market_documents(db, *, days: int, limit: int) -> list[dict[str, Any]]:
    since = (date.today() - timedelta(days=max(0, int(days or 0)))).isoformat()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT doc_uid, stock_code, doc_type, source, source_tier, provider, original_id,
                   title, summary, body, url, published_at, fetched_at, checksum,
                   reliability_score, crawl_status, metadata
            FROM market_documents
            WHERE doc_type IN ('notice', 'news')
              AND COALESCE(substr(published_at, 1, 10), substr(fetched_at, 1, 10), '') >= $1
            ORDER BY COALESCE(published_at, fetched_at, created_at) DESC
            LIMIT $2
            """,
            since,
            max(1, min(int(limit or 200), 2000)),
        )
    docs: list[dict[str, Any]] = []
    decoder = getattr(db, "_decode_json_field", None)
    for row in rows:
        item = dict(row)
        if callable(decoder):
            item["metadata"] = decoder(item.get("metadata"), {})
        docs.append(item)
    return docs


async def _persist_rss_documents(db, docs: list[dict[str, Any]], *, embed: bool) -> dict[str, Any]:
    if not docs:
        return {"documents": 0, "feeds": 0}
    saved = await db.save_market_documents(
        "MARKET",
        "news",
        docs,
        embed=embed,
        chunk_size=1000,
        overlap=120,
        version="radar_v1",
    )
    try:
        from ..market_event_sources import persist_normalized_events

        normalized = await persist_normalized_events(db, "MARKET", "news", docs)
    except Exception as exc:
        normalized = {"error": f"{type(exc).__name__}: {exc}"}
    return {**saved, "normalized_events": normalized}
