"""Lightweight text document normalization helpers for retrieval-oriented tools."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _make_doc_id(doc_type: str, date_value: str, title: str, source: str, text: str) -> str:
    raw = "|".join([doc_type, date_value, title, source, text[:256]])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def normalize_documents(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        doc_type = _normalize_text(item.get("type") or item.get("doc_type") or "text") or "text"
        date_value = _normalize_text(item.get("date"))
        title = _normalize_text(item.get("title") or item.get("headline") or item.get("name"))
        source = _normalize_text(item.get("source") or "unknown")
        text = _normalize_text(item.get("text") or item.get("content") or item.get("summary"))
        if not any([title, text, source]):
            continue
        documents.append(
            {
                "doc_id": _make_doc_id(doc_type, date_value, title, source, text),
                "doc_type": doc_type,
                "date": date_value or None,
                "title": title,
                "source": source,
                "text": text,
                "text_length": len(text),
                "is_empty_text": not bool(text),
            }
        )
    return documents


def build_document_index(items: list[dict[str, Any]]) -> dict[str, Any]:
    documents = normalize_documents(items)
    type_counter = Counter(doc.get("doc_type") or "unknown" for doc in documents)
    source_counter = Counter(doc.get("source") or "unknown" for doc in documents)
    dated_documents = [doc for doc in documents if doc.get("date")]
    sorted_documents = sorted(dated_documents, key=lambda item: item.get("date") or "", reverse=True)
    return {
        "documents": documents,
        "stats": {
            "total_documents": len(documents),
            "non_empty_texts": sum(1 for doc in documents if doc.get("text")),
            "doc_type_counts": dict(type_counter),
            "source_counts": dict(source_counter),
            "latest_dates": [doc.get("date") for doc in sorted_documents[:5]],
        },
    }
