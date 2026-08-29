"""Unified vector-layer schema for market / quant / strategy derived objects."""

from __future__ import annotations

import json
import logging

from aiask_quant_core.vector_collection_scope import KLINE_COLLECTION_SPECS, market_doc_collection_name

logger = logging.getLogger(__name__)


async def _seed_vector_collection(
    conn,
    *,
    collection_name: str,
    entity_family: str,
    backend: str,
    metric: str,
    model_id: str,
    vector_dim: int,
    normalization: str = "unit",
    status: str = "active",
    metadata: dict | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO vector_collections (
            collection_name, entity_family, backend, metric, model_id,
            vector_dim, normalization, status, metadata, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (collection_name) DO NOTHING
        """,
        collection_name,
        entity_family,
        backend,
        metric,
        model_id,
        int(vector_dim),
        normalization,
        status,
        json.dumps(metadata or {}, ensure_ascii=False, default=str),
    )


async def _seed_vector_dimension_contract(
    conn,
    *,
    collection_name: str,
    entity_family: str,
    vector_dim: int,
    model_id: str | None = None,
    profile_type: str | None = None,
    version_prefix: str = "",
    metric: str = "cosine",
    status: str = "active",
    metadata: dict | None = None,
) -> None:
    await conn.execute(
        """
        INSERT OR REPLACE INTO vector_dimension_contracts (
            collection_name, entity_family, profile_type, model_id, vector_dim,
            version_prefix, metric, status, metadata, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        collection_name,
        entity_family,
        profile_type,
        model_id,
        int(vector_dim),
        str(version_prefix or ""),
        metric,
        status,
        json.dumps(metadata or {}, ensure_ascii=False, default=str),
    )


async def init_vector_tables(conn, sqlite_python_enabled: bool) -> None:
    """Create / migrate generic vector-layer tables."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_collections (
            id INTEGER PRIMARY KEY,
            collection_name TEXT NOT NULL UNIQUE,
            entity_family TEXT NOT NULL,
            backend TEXT NOT NULL DEFAULT 'sqlite_python',
            metric TEXT NOT NULL DEFAULT 'cosine',
            model_id TEXT NOT NULL,
            vector_dim INTEGER NOT NULL,
            normalization TEXT NOT NULL DEFAULT 'unit',
            status TEXT NOT NULL DEFAULT 'active',
            active_version TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_vector_collections_family
            ON vector_collections(entity_family, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_vector_collections_model
            ON vector_collections(model_id, vector_dim, metric);

        CREATE TABLE IF NOT EXISTS vector_dimension_contracts (
            id INTEGER PRIMARY KEY,
            collection_name TEXT NOT NULL,
            entity_family TEXT NOT NULL,
            profile_type TEXT,
            model_id TEXT,
            vector_dim INTEGER NOT NULL,
            version_prefix TEXT NOT NULL DEFAULT '',
            metric TEXT NOT NULL DEFAULT 'cosine',
            status TEXT NOT NULL DEFAULT 'active',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_vector_dimension_contracts_key
            ON vector_dimension_contracts(collection_name, COALESCE(profile_type, ''), COALESCE(model_id, ''), version_prefix);
        CREATE INDEX IF NOT EXISTS idx_vector_dimension_contracts_lookup
            ON vector_dimension_contracts(collection_name, status, profile_type, model_id, vector_dim);

        CREATE TABLE IF NOT EXISTS vector_profiles (
            id INTEGER PRIMARY KEY,
            collection_name TEXT NOT NULL REFERENCES vector_collections(collection_name) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            stock_code TEXT,
            profile_type TEXT,
            model_id TEXT NOT NULL,
            vector_dim INTEGER NOT NULL,
            metric TEXT NOT NULL DEFAULT 'cosine',
            version TEXT NOT NULL,
            signature TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            embedding_json TEXT NOT NULL DEFAULT '[]',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (collection_name, entity_type, entity_id, model_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_vector_profiles_lookup
            ON vector_profiles(collection_name, version, profile_type, vector_dim, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_vector_profiles_stock
            ON vector_profiles(stock_code, collection_name, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_vector_profiles_signature
            ON vector_profiles(signature);
        CREATE TABLE IF NOT EXISTS vector_index_snapshots (
            id INTEGER PRIMARY KEY,
            collection_name TEXT NOT NULL REFERENCES vector_collections(collection_name) ON DELETE CASCADE,
            index_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'building',
            model_id TEXT NOT NULL,
            profile_type TEXT,
            metric TEXT NOT NULL DEFAULT 'cosine',
            vector_dim INTEGER NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            bucket_count INTEGER NOT NULL DEFAULT 0,
            index_params TEXT NOT NULL DEFAULT '{}',
            metrics TEXT NOT NULL DEFAULT '{}',
            metadata TEXT NOT NULL DEFAULT '{}',
            built_at TEXT,
            activated_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (collection_name, index_version)
        );
        CREATE INDEX IF NOT EXISTS idx_vector_index_snapshots_lookup
            ON vector_index_snapshots(collection_name, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_vector_index_snapshots_model
            ON vector_index_snapshots(model_id, vector_dim, metric);

        CREATE TABLE IF NOT EXISTS vector_index_items (
            id INTEGER PRIMARY KEY,
            collection_name TEXT NOT NULL REFERENCES vector_collections(collection_name) ON DELETE CASCADE,
            index_version TEXT NOT NULL,
            profile_id INTEGER REFERENCES vector_profiles(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            stock_code TEXT,
            profile_type TEXT,
            model_id TEXT NOT NULL,
            metric TEXT NOT NULL DEFAULT 'cosine',
            vector_dim INTEGER NOT NULL,
            bucket_id TEXT,
            coarse_score REAL NOT NULL DEFAULT 0,
            embedding_json TEXT NOT NULL DEFAULT '[]',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (collection_name, index_version, profile_id)
        );
        CREATE INDEX IF NOT EXISTS idx_vector_index_items_lookup
            ON vector_index_items(collection_name, index_version, bucket_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_vector_index_items_stock
            ON vector_index_items(stock_code, collection_name, created_at DESC);
        CREATE TABLE IF NOT EXISTS market_documents (
            id INTEGER PRIMARY KEY,
            doc_uid TEXT NOT NULL UNIQUE,
            stock_code TEXT,
            doc_type TEXT NOT NULL,
            source TEXT NOT NULL,
            source_tier TEXT NOT NULL DEFAULT 'tier_c',
            provider TEXT,
            original_id TEXT,
            title TEXT,
            summary TEXT,
            body TEXT,
            url TEXT,
            author TEXT,
            published_at TEXT,
            fetched_at TEXT,
            checksum TEXT,
            reliability_score REAL DEFAULT 0,
            crawl_status TEXT NOT NULL DEFAULT 'ok',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ALTER TABLE market_documents ADD COLUMN IF NOT EXISTS source_tier TEXT NOT NULL DEFAULT 'tier_c';
        ALTER TABLE market_documents ADD COLUMN IF NOT EXISTS provider TEXT;
        ALTER TABLE market_documents ADD COLUMN IF NOT EXISTS original_id TEXT;
        ALTER TABLE market_documents ADD COLUMN IF NOT EXISTS fetched_at TEXT;
        ALTER TABLE market_documents ADD COLUMN IF NOT EXISTS checksum TEXT;
        ALTER TABLE market_documents ADD COLUMN IF NOT EXISTS reliability_score REAL DEFAULT 0;
        ALTER TABLE market_documents ADD COLUMN IF NOT EXISTS crawl_status TEXT NOT NULL DEFAULT 'ok';
        CREATE INDEX IF NOT EXISTS idx_market_documents_lookup
            ON market_documents(stock_code, doc_type, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_market_documents_source
            ON market_documents(source, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_market_documents_provenance
            ON market_documents(source_tier, provider, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_market_documents_checksum
            ON market_documents(checksum);
        CREATE TABLE IF NOT EXISTS market_events_normalized (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            event_name TEXT NOT NULL,
            summary TEXT,
            entity_codes TEXT NOT NULL DEFAULT '[]',
            theme_codes TEXT NOT NULL DEFAULT '[]',
            direction TEXT NOT NULL DEFAULT 'neutral',
            event_time TEXT,
            publish_time TEXT,
            evidence_time TEXT,
            source_doc_uids TEXT NOT NULL DEFAULT '[]',
            source_tier TEXT NOT NULL DEFAULT 'tier_c',
            source_types TEXT NOT NULL DEFAULT '[]',
            provider_chain TEXT NOT NULL DEFAULT '[]',
            reliability_score REAL NOT NULL DEFAULT 0,
            cross_source_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'provisional',
            reject_reason TEXT,
            freshness_status TEXT NOT NULL DEFAULT 'unknown',
            event_anchor_id TEXT,
            checksum TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_market_events_normalized_status
            ON market_events_normalized(status, evidence_time DESC);
        CREATE INDEX IF NOT EXISTS idx_market_events_normalized_tier
            ON market_events_normalized(source_tier, reliability_score DESC, evidence_time DESC);
        CREATE INDEX IF NOT EXISTS idx_market_events_normalized_type
            ON market_events_normalized(event_type, evidence_time DESC);
        CREATE INDEX IF NOT EXISTS idx_market_events_normalized_checksum
            ON market_events_normalized(checksum);
        CREATE TABLE IF NOT EXISTS stock_radar_runs (
            run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'dry_run',
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            summary TEXT NOT NULL DEFAULT '{}',
            degraded_flags TEXT NOT NULL DEFAULT '[]',
            error TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_stock_radar_runs_status
            ON stock_radar_runs(status, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_stock_radar_runs_mode
            ON stock_radar_runs(mode, started_at DESC);

        CREATE TABLE IF NOT EXISTS stock_radar_candidates (
            candidate_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES stock_radar_runs(run_id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            stock_name TEXT,
            tier TEXT NOT NULL DEFAULT 'observe',
            radar_score REAL NOT NULL DEFAULT 0,
            event_id TEXT,
            event_type TEXT NOT NULL DEFAULT 'unknown',
            direction TEXT NOT NULL DEFAULT 'neutral',
            summary TEXT,
            source_doc_uids TEXT NOT NULL DEFAULT '[]',
            source_chain TEXT NOT NULL DEFAULT '[]',
            extraction TEXT NOT NULL DEFAULT '{}',
            confirmations TEXT NOT NULL DEFAULT '{}',
            risk_flags TEXT NOT NULL DEFAULT '[]',
            push_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_stock_radar_candidates_run
            ON stock_radar_candidates(run_id, radar_score DESC);
        CREATE INDEX IF NOT EXISTS idx_stock_radar_candidates_tier
            ON stock_radar_candidates(tier, radar_score DESC, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_stock_radar_candidates_symbol
            ON stock_radar_candidates(symbol, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_stock_radar_candidates_event
            ON stock_radar_candidates(event_id);

        CREATE TABLE IF NOT EXISTS stock_radar_push_logs (
            push_id TEXT PRIMARY KEY,
            run_id TEXT REFERENCES stock_radar_runs(run_id) ON DELETE SET NULL,
            channel TEXT NOT NULL,
            platform TEXT,
            target TEXT,
            status TEXT NOT NULL DEFAULT 'preview',
            message_preview TEXT,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_stock_radar_push_logs_run
            ON stock_radar_push_logs(run_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_stock_radar_push_logs_status
            ON stock_radar_push_logs(status, created_at DESC);
        CREATE TABLE IF NOT EXISTS market_doc_chunks (
            id INTEGER PRIMARY KEY,
            doc_id INTEGER NOT NULL REFERENCES market_documents(id) ON DELETE CASCADE,
            chunk_no INTEGER NOT NULL,
            stock_code TEXT,
            doc_type TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT,
            chunk_text TEXT NOT NULL,
            token_count INTEGER,
            char_count INTEGER,
            language TEXT DEFAULT 'zh',
            published_at TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (doc_id, chunk_no)
        );
        CREATE INDEX IF NOT EXISTS idx_market_doc_chunks_lookup
            ON market_doc_chunks(stock_code, doc_type, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_market_doc_chunks_doc
            ON market_doc_chunks(doc_id, chunk_no);
        CREATE INDEX IF NOT EXISTS idx_market_doc_chunks_source
            ON market_doc_chunks(source, published_at DESC);
        CREATE TABLE IF NOT EXISTS kline_pattern_windows (
            id INTEGER PRIMARY KEY,
            window_uid TEXT NOT NULL UNIQUE,
            stock_code TEXT NOT NULL,
            end_date TEXT NOT NULL,
            start_date TEXT NOT NULL,
            period TEXT NOT NULL DEFAULT 'daily',
            adjust TEXT NOT NULL DEFAULT '',
            window_size INTEGER NOT NULL,
            vector_method TEXT NOT NULL,
            metric TEXT NOT NULL DEFAULT 'cosine',
            vector_dim INTEGER NOT NULL,
            forward_return_5d REAL,
            forward_return_10d REAL,
            forward_return_20d REAL,
            payload TEXT NOT NULL DEFAULT '{}',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_kline_pattern_windows_lookup
            ON kline_pattern_windows(stock_code, period, adjust, end_date DESC);
        CREATE INDEX IF NOT EXISTS idx_kline_pattern_windows_method
            ON kline_pattern_windows(vector_method, window_size, vector_dim, end_date DESC);

        CREATE VIRTUAL TABLE IF NOT EXISTS market_doc_chunks_fts USING fts5(
            title,
            chunk_text,
            stock_code UNINDEXED,
            doc_type UNINDEXED,
            source UNINDEXED,
            content='market_doc_chunks',
            content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS market_doc_chunks_fts_ai AFTER INSERT ON market_doc_chunks BEGIN
            INSERT INTO market_doc_chunks_fts(rowid, title, chunk_text, stock_code, doc_type, source)
            VALUES (new.id, new.title, new.chunk_text, new.stock_code, new.doc_type, new.source);
        END;
        CREATE TRIGGER IF NOT EXISTS market_doc_chunks_fts_ad AFTER DELETE ON market_doc_chunks BEGIN
            INSERT INTO market_doc_chunks_fts(market_doc_chunks_fts, rowid, title, chunk_text, stock_code, doc_type, source)
            VALUES ('delete', old.id, old.title, old.chunk_text, old.stock_code, old.doc_type, old.source);
        END;
        CREATE TRIGGER IF NOT EXISTS market_doc_chunks_fts_au AFTER UPDATE ON market_doc_chunks BEGIN
            INSERT INTO market_doc_chunks_fts(market_doc_chunks_fts, rowid, title, chunk_text, stock_code, doc_type, source)
            VALUES ('delete', old.id, old.title, old.chunk_text, old.stock_code, old.doc_type, old.source);
            INSERT INTO market_doc_chunks_fts(rowid, title, chunk_text, stock_code, doc_type, source)
            VALUES (new.id, new.title, new.chunk_text, new.stock_code, new.doc_type, new.source);
        END;

        CREATE TABLE IF NOT EXISTS vector_graph_nodes (
            node_key TEXT PRIMARY KEY,
            node_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            stock_code TEXT,
            label TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_vector_graph_nodes_type
            ON vector_graph_nodes(node_type, stock_code, updated_at DESC);
        CREATE TABLE IF NOT EXISTS vector_graph_edges (
            edge_key TEXT PRIMARY KEY,
            source_node_key TEXT NOT NULL,
            target_node_key TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_vector_graph_edges_source
            ON vector_graph_edges(source_node_key, relation_type, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_vector_graph_edges_target
            ON vector_graph_edges(target_node_key, relation_type, updated_at DESC);

        CREATE TABLE IF NOT EXISTS vector_optimization_runs (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'running',
            scope TEXT NOT NULL DEFAULT 'full',
            cursor TEXT,
            next_cursor TEXT,
            batch_size INTEGER NOT NULL DEFAULT 500,
            dry_run INTEGER NOT NULL DEFAULT 0,
            stats TEXT NOT NULL DEFAULT '{}',
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_vector_optimization_runs_lookup
            ON vector_optimization_runs(status, created_at DESC);
        """
    )

    if sqlite_python_enabled:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_profile_store (
                profile_id INTEGER PRIMARY KEY REFERENCES vector_profiles(id) ON DELETE CASCADE,
                collection_name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                stock_code TEXT,
                profile_type TEXT,
                model_id TEXT NOT NULL,
                vector_dim INTEGER NOT NULL,
                metric TEXT NOT NULL DEFAULT 'cosine',
                version TEXT NOT NULL,
                embedding TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_vector_profile_store_lookup
                ON vector_profile_store(collection_name, version, profile_type, vector_dim, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_vector_profile_store_stock
                ON vector_profile_store(stock_code, collection_name, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_vector_profile_store_model
                ON vector_profile_store(model_id, vector_dim, metric, version);
            CREATE TABLE IF NOT EXISTS vector_index_item_store (
                item_id INTEGER PRIMARY KEY REFERENCES vector_index_items(id) ON DELETE CASCADE,
                collection_name TEXT NOT NULL,
                index_version TEXT NOT NULL,
                profile_id INTEGER,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                stock_code TEXT,
                profile_type TEXT,
                model_id TEXT NOT NULL,
                metric TEXT NOT NULL DEFAULT 'cosine',
                vector_dim INTEGER NOT NULL,
                bucket_id TEXT,
                embedding TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            ALTER TABLE vector_index_item_store
                ADD COLUMN IF NOT EXISTS bucket_id TEXT;
            CREATE INDEX IF NOT EXISTS idx_vector_index_item_store_lookup
                ON vector_index_item_store(collection_name, index_version, profile_type, vector_dim, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_vector_index_item_store_bucket_lookup
                ON vector_index_item_store(collection_name, index_version, bucket_id, profile_type, vector_dim, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_vector_index_item_store_stock
                ON vector_index_item_store(stock_code, collection_name, updated_at DESC);
            """
        )

    try:
        await _seed_vector_collection(
            conn,
            collection_name="market_doc_chunks",
            entity_family="document_chunk",
            backend="sqlite_python" if sqlite_python_enabled else "index",
            metric="cosine",
            model_id="text-embedding-3-small",
            vector_dim=1536,
            metadata={"domain": "market", "notes": "news-notice-research chunks"},
        )
        await _seed_vector_collection(
            conn,
            collection_name="kline_pattern_embeddings",
            entity_family="kline_pattern",
            backend="sqlite_python" if sqlite_python_enabled else "index",
            metric="cosine",
            model_id="price_volume_v1",
            vector_dim=120,
            metadata={"domain": "quant", "notes": "fixed-window pattern vectors"},
        )
        await _seed_vector_collection(
            conn,
            collection_name="stock_profile_embeddings",
            entity_family="stock_profile",
            backend="sqlite_python" if sqlite_python_enabled else "index",
            metric="cosine",
            model_id="stock-profile-v1",
            vector_dim=11,
            metadata={"domain": "market-quant", "notes": "derived stock profile vectors"},
        )
        await _seed_vector_collection(
            conn,
            collection_name="factor_candidate_embeddings",
            entity_family="factor_candidate",
            backend="sqlite_python" if sqlite_python_enabled else "index",
            metric="cosine",
            model_id="factor-memory-v1",
            vector_dim=128,
            metadata={"domain": "quant-research", "notes": "factor candidate memory"},
        )
        await _seed_vector_collection(
            conn,
            collection_name="strategy_behavior_embeddings",
            entity_family="strategy_behavior",
            backend="sqlite_python" if sqlite_python_enabled else "index",
            metric="cosine",
            model_id="strategy-behavior-v1",
            vector_dim=120,
            metadata={"domain": "strategy", "notes": "strategy behavior vectors"},
        )
        for spec in KLINE_COLLECTION_SPECS.values():
            await _seed_vector_collection(
                conn,
                collection_name=str(spec["collection_name"]),
                entity_family="kline_pattern",
                backend="sqlite_python" if sqlite_python_enabled else "index",
                metric="cosine",
                model_id=str(spec["model_id"]),
                vector_dim=int(spec["vector_dim"]),
                metadata={"domain": "quant", "notes": "dimension-scoped kline pattern vectors"},
            )
        await _seed_vector_dimension_contract(
            conn,
            collection_name="stock_profile_embeddings",
            entity_family="stock_profile",
            model_id="stock-profile-v1",
            vector_dim=11,
            version_prefix="stock-profile-v1",
            metadata={"feature_set": "fundamental|technical|both"},
        )
        await _seed_vector_dimension_contract(
            conn,
            collection_name="factor_candidate_embeddings",
            entity_family="factor_candidate",
            model_id="factor-memory-v1",
            vector_dim=128,
            metadata={"feature_set": "hashed_token_ngram"},
        )
        await _seed_vector_dimension_contract(
            conn,
            collection_name="strategy_behavior_embeddings",
            entity_family="strategy_behavior",
            model_id="strategy-behavior-v1",
            vector_dim=120,
            metadata={"feature_set": "numeric_behavior_price_volume"},
        )
        for spec in KLINE_COLLECTION_SPECS.values():
            await _seed_vector_dimension_contract(
                conn,
                collection_name=str(spec["collection_name"]),
                entity_family="kline_pattern",
                model_id=str(spec["model_id"]),
                vector_dim=int(spec["vector_dim"]),
                metadata={"feature_set": "kline_pattern"},
            )
        for doc_type in ("news", "notice", "research"):
            collection = market_doc_collection_name(doc_type)
            for vector_dim in (1536, 256):
                await _seed_vector_dimension_contract(
                    conn,
                    collection_name=collection,
                    entity_family="document_chunk",
                    profile_type=doc_type,
                    model_id="text-embedding-3-small",
                    vector_dim=vector_dim,
                    version_prefix=f"v1__d{vector_dim}",
                    metadata={"doc_type": doc_type, "feature_set": "text_embedding_or_hash_fallback"},
                )
    except Exception as exc:
        logger.warning("vector collection seed skipped: %s", exc)

    logger.info("Vector tables initialized (sqlite_python=%s)", sqlite_python_enabled)
