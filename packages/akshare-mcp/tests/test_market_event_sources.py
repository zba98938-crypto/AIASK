from __future__ import annotations

from akshare_mcp.services.market_event_sources import (
    bridge_normalized_events_to_strategy_factory,
    event_source_status,
    fetch_cninfo_official_announcements,
    fetch_official_market_event_documents,
    fetch_sse_official_announcements,
    normalize_market_text_events,
    persist_normalized_events,
)


class _BridgeDb:
    def __init__(self, events: list[dict]):
        self.events = events
        self.clusters: list[dict] = []
        self.signals: list[dict] = []

    async def list_market_events_normalized(self, status: str | None = None, limit: int = 50):
        rows = [
            item for item in self.events
            if status is None or str(item.get("status") or "") == status
        ]
        return rows[:limit]

    async def save_factory_event_cluster(self, item: dict):
        self.clusters.append(dict(item))
        return dict(item)

    async def save_factory_event_signal(self, item: dict):
        self.signals.append(dict(item))
        return dict(item)


class _PersistBridgeDb(_BridgeDb):
    async def list_market_events_normalized(
        self,
        status: str | None = None,
        event_signature: str | None = None,
        limit: int = 50,
    ):
        rows = [
            item for item in self.events
            if status is None or str(item.get("status") or "") == status
        ]
        if event_signature:
            rows = [
                item for item in rows
                if str((item.get("metadata") or {}).get("event_signature") or "") == event_signature
            ]
        return rows[:limit]

    async def upsert_market_event_normalized(self, item: dict):
        payload = dict(item)
        event_id = str(payload.get("event_id") or "")
        for idx, existing in enumerate(self.events):
            if str(existing.get("event_id") or "") == event_id:
                self.events[idx] = payload
                return payload
        self.events.append(payload)
        return payload


def test_tier_c_market_news_is_provisional_not_verified():
    events = normalize_market_text_events(
        object(),
        "000001",
        "news",
        [
            {
                "title": "Media says 000001 may benefit from policy",
                "summary": "Single source media headline.",
                "date": "2026-06-06",
                "provider": "eastmoney_finance_news",
                "source_tier": "tier_c",
            }
        ],
    )

    assert len(events) == 1
    event = events[0]
    assert event["source_tier"] == "tier_c"
    assert event["status"] == "provisional"
    assert event["reject_reason"] == "news_only_or_low_tier_source"


def test_event_source_status_marks_unimplemented_sources_degraded(monkeypatch):
    monkeypatch.delenv("WIND_TOKEN", raising=False)

    status = event_source_status()
    by_name = {item["name"]: item for item in status["adapters"]}

    assert by_name["cninfo"]["implemented"] is True
    assert by_name["cninfo"]["degraded"] is False
    assert by_name["sse"]["implemented"] is True
    assert by_name["sse"]["degraded"] is False
    assert by_name["szse"]["implemented"] is False
    assert by_name["szse"]["degraded"] is True
    assert by_name["wind"]["implemented"] is False
    assert by_name["wind"]["degraded"] is True


def test_fetch_sse_official_announcements_maps_tier_a_documents(monkeypatch):
    class FakeResponse:
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "pageHelp": {
                    "data": [
                        {
                            "SECURITY_CODE": "600519",
                            "SECURITY_NAME": "贵州茅台",
                            "SSEDATE": "2026-06-08",
                            "TITLE": "贵州茅台关于调整利润分配方案的公告",
                            "BULLETIN_TYPE": "临时公告",
                            "URL": "/disclosure/listedinfo/announcement/c/new/2026-06-08/600519_mock.pdf",
                        }
                    ],
                    "total": 1,
                }
            }

    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params or {}))
        return FakeResponse()

    monkeypatch.setattr("akshare_mcp.services.market_event_sources.requests.get", fake_get)

    rows = fetch_sse_official_announcements("2026-06-01", "2026-06-08", limit=5, stock_codes=["600519"])

    assert calls[0]["productId"] == "600519"
    assert rows[0]["provider"] == "sse"
    assert rows[0]["source_tier"] == "tier_a"
    assert rows[0]["stock_code"] == "600519"
    assert rows[0]["url"].startswith("https://static.sse.com.cn/")


def test_fetch_official_documents_uses_sse_and_keeps_other_exchanges_degraded(monkeypatch):
    monkeypatch.setattr("akshare_mcp.services.market_event_sources.fetch_cninfo_official_announcements", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "akshare_mcp.services.market_event_sources.fetch_sse_official_announcements",
        lambda *args, **kwargs: [
            {
                "doc_uid": "sse:1",
                "title": "SSE official notice",
                "stock_code": "600519",
                "source_tier": "tier_a",
                "provider": "sse",
            }
        ],
    )

    result = fetch_official_market_event_documents(
        "2026-06-01",
        "2026-06-08",
        providers=["cninfo", "sse", "szse", "bse"],
        limit=5,
    )

    assert result["items"] == [
        {
            "doc_uid": "sse:1",
            "title": "SSE official notice",
            "stock_code": "600519",
            "source_tier": "tier_a",
            "provider": "sse",
        }
    ]
    assert result["sources"]["sse"]["status"] == "ok"
    assert result["sources"]["szse"]["reason"] == "official_source_adapter_pending"
    assert result["sources"]["bse"]["reason"] == "official_source_adapter_pending"


def test_tier_a_official_notice_bridges_with_verified_anchor():
    events = normalize_market_text_events(
        object(),
        "000001",
        "notice",
        [
            {
                "title": "000001 signs major order contract",
                "summary": "Official disclosure for a new signed order contract.",
                "date": "2026-06-06",
                "provider": "cninfo",
                "source_tier": "tier_a",
                "doc_uid": "cninfo:000001:2026-06-06:order",
            }
        ],
    )
    assert len(events) == 1
    assert events[0]["status"] == "verified"
    assert events[0]["source_tier"] == "tier_a"

    db = _BridgeDb(events)

    import asyncio

    result = asyncio.run(bridge_normalized_events_to_strategy_factory(db))

    assert result["enabled"] is True
    assert result["bridged_events"] == 1
    assert result["signals"] == 1
    assert db.clusters[0]["evidence"]["source"] == "market_events_normalized"
    assert db.clusters[0]["evidence"]["verified_event_anchor"] is True
    assert db.clusters[0]["evidence"]["source_doc_uids"] == ["cninfo:000001:2026-06-06:order"]
    assert db.clusters[0]["evidence"]["source_tier"] == "tier_a"
    assert db.clusters[0]["confidence"] <= 0.65
    assert db.clusters[0]["evidence"]["alpha_confirmation_status"] == "single_anchor_unconfirmed"
    assert db.clusters[0]["evidence"]["needs_alpha_confirmation"] is True


def test_persist_single_official_anchor_caps_alpha_confidence():
    import asyncio

    db = _PersistBridgeDb([])
    summary = asyncio.run(
        persist_normalized_events(
            db,
            "000001",
            "notice",
            [
                {
                    "title": "000001 signs major order contract",
                    "summary": "Official disclosure for a new signed order contract.",
                    "date": "2026-06-06",
                    "provider": "cninfo",
                    "source_tier": "tier_a",
                    "doc_uid": "cninfo:000001:2026-06-06:order",
                }
            ],
        )
    )

    assert summary["verified"] == 1
    event = db.events[0]
    validation = event["metadata"]["validation_summary"]
    assert validation["occurrence_status"] == "verified_single_anchor"
    assert validation["alpha_confirmation_status"] == "single_anchor_unconfirmed"
    assert event["reliability_score"] == 0.65
    assert validation["confidence_cap_reason"] == "single_official_or_institutional_anchor"


def test_cross_ingest_multisource_confirmation_merges_existing_signature():
    import asyncio

    db = _PersistBridgeDb([])
    asyncio.run(
        persist_normalized_events(
            db,
            "000001",
            "notice",
            [
                {
                    "title": "000001 signs major order contract",
                    "summary": "Official disclosure for a new signed order contract.",
                    "date": "2026-06-06",
                    "provider": "cninfo",
                    "source_tier": "tier_a",
                    "doc_uid": "cninfo:000001:2026-06-06:order",
                }
            ],
        )
    )
    asyncio.run(
        persist_normalized_events(
            db,
            "000001",
            "news",
            [
                {
                    "title": "Eastmoney says 000001 signs major order contract",
                    "summary": "Media follow-up for the same signed order contract.",
                    "date": "2026-06-06",
                    "provider": "eastmoney",
                    "source_tier": "tier_c",
                    "doc_uid": "eastmoney:000001:2026-06-06:order",
                }
            ],
        )
    )

    assert len(db.events) == 1
    event = db.events[0]
    validation = event["metadata"]["validation_summary"]
    assert validation["occurrence_status"] == "verified_multi_source"
    assert validation["alpha_confirmation_status"] == "confirmed"
    assert validation["cross_source_count"] == 2
    assert validation["official_anchor_count"] == 1
    assert validation["media_confirm_count"] == 1
    assert event["source_doc_uids"] == [
        "cninfo:000001:2026-06-06:order",
        "eastmoney:000001:2026-06-06:order",
    ]
    assert event["provider_chain"] == ["cninfo", "eastmoney"]
    assert event["reliability_score"] == 0.92


def test_duplicate_official_anchor_does_not_fake_multisource_confirmation():
    import asyncio

    db = _PersistBridgeDb([])
    item = {
        "title": "000001 signs major order contract",
        "summary": "Official disclosure for a new signed order contract.",
        "date": "2026-06-06",
        "provider": "cninfo",
        "source_tier": "tier_a",
        "doc_uid": "cninfo:000001:2026-06-06:order",
    }
    asyncio.run(persist_normalized_events(db, "000001", "notice", [item]))
    asyncio.run(persist_normalized_events(db, "000001", "notice", [item]))

    event = db.events[0]
    validation = event["metadata"]["validation_summary"]
    assert validation["cross_source_count"] == 1
    assert validation["alpha_confirmation_status"] == "single_anchor_unconfirmed"
    assert event["reliability_score"] == 0.65


def test_conflicted_verified_event_bridges_as_diagnostic_without_signals():
    import asyncio

    db = _PersistBridgeDb([])
    asyncio.run(
        persist_normalized_events(
            db,
            "000001",
            "notice",
            [
                {
                    "title": "000001 signs major order contract",
                    "summary": "Official disclosure for a new signed order contract.",
                    "date": "2026-06-06",
                    "provider": "cninfo",
                    "source_tier": "tier_a",
                    "doc_uid": "cninfo:000001:2026-06-06:order",
                }
            ],
        )
    )
    asyncio.run(
        persist_normalized_events(
            db,
            "000001",
            "news",
            [
                {
                    "title": "Eastmoney says 000001 signs major order contract",
                    "summary": "Media disputes the same signed order contract.",
                    "date": "2026-06-06",
                    "provider": "eastmoney",
                    "source_tier": "tier_c",
                    "doc_uid": "eastmoney:000001:2026-06-06:order-risk",
                    "direction": "down",
                }
            ],
        )
    )

    event = db.events[0]
    validation = event["metadata"]["validation_summary"]
    assert event["direction"] == "neutral"
    assert validation["alpha_confirmation_status"] == "conflicted"
    assert validation["conflict_count"] == 1

    result = asyncio.run(bridge_normalized_events_to_strategy_factory(db))

    assert result["bridged_events"] == 0
    assert result["diagnostic_events"] == 1
    assert result["signals"] == 0
    assert db.clusters[0]["status"] == "diagnostic"
    assert db.clusters[0]["evidence"]["alpha_confirmation_status"] == "conflicted"
    assert db.signals == []


def test_cninfo_official_fetch_maps_to_tier_a_documents(monkeypatch):
    class _Response:
        status_code = 200
        text = "ok"

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "totalAnnouncement": 1,
                "announcements": [
                    {
                        "secCode": "000001",
                        "secName": "Ping An Bank",
                        "announcementId": "1225000001",
                        "announcementTitle": "Ping An Bank signs major order contract",
                        "announcementTime": 1780675200000,
                        "adjunctUrl": "finalpage/2026-06-06/1225000001.PDF",
                        "announcementType": "major",
                    }
                ],
            }

    import akshare_mcp.services.market_event_sources as mod

    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": dict(data or {}), "timeout": timeout})
        return _Response()

    monkeypatch.setattr(mod.requests, "post", fake_post)

    rows = fetch_cninfo_official_announcements(
        "2026-06-01",
        "2026-06-06",
        limit=5,
        stock_codes=["000001"],
    )

    assert len(rows) == 1
    assert calls[0]["data"]["searchkey"] == "000001"
    assert rows[0]["source_tier"] == "tier_a"
    assert rows[0]["provider"] == "cninfo"
    assert rows[0]["doc_uid"] == "cninfo:1225000001"
    assert rows[0]["code"] == "000001"
    assert rows[0]["url"].startswith("https://static.cninfo.com.cn/finalpage/")


def test_official_fetch_degrades_pending_sources_but_keeps_cninfo(monkeypatch):
    import akshare_mcp.services.market_event_sources as mod

    monkeypatch.setattr(
        mod,
        "fetch_cninfo_official_announcements",
        lambda *args, **kwargs: [{"doc_uid": "cninfo:1", "code": "000001", "source_tier": "tier_a"}],
    )
    monkeypatch.setattr(mod, "fetch_sse_official_announcements", lambda *args, **kwargs: [])

    result = fetch_official_market_event_documents(
        "2026-06-01",
        "2026-06-06",
        limit=5,
        stock_codes=["000001"],
        providers=["cninfo", "sse"],
    )

    assert result["items"] == [{"doc_uid": "cninfo:1", "code": "000001", "source_tier": "tier_a"}]
    assert result["sources"]["cninfo"]["status"] == "ok"
    assert result["sources"]["sse"]["status"] == "ok"
    assert result["sources"]["sse"]["fetched"] == 0


def test_bridge_skips_news_only_or_low_tier_events():
    db = _BridgeDb(
        [
            {
                "event_id": "tier_c_evt",
                "event_type": "market_news",
                "event_name": "media only event",
                "summary": "media only",
                "entity_codes": ["000001"],
                "theme_codes": ["market_news"],
                "direction": "up",
                "source_doc_uids": ["eastmoney:news:1"],
                "source_tier": "tier_c",
                "source_types": ["eastmoney"],
                "provider_chain": ["eastmoney"],
                "reliability_score": 0.45,
                "cross_source_count": 1,
                "status": "verified",
                "event_anchor_id": "tier_c_evt",
            }
        ]
    )

    import asyncio

    result = asyncio.run(bridge_normalized_events_to_strategy_factory(db))

    assert result["bridged_events"] == 0
    assert result["skipped"] == 1
    assert db.clusters == []
    assert db.signals == []
