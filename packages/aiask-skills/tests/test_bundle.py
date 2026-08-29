from __future__ import annotations

from aiask_skills import PROTOCOL_VERSION, bundle_path, discover, skill_paths


def test_bundle_contains_versioned_host_neutral_skills() -> None:
    paths = skill_paths()
    assert len(paths) >= 19
    assert bundle_path().is_dir()
    records = discover()
    assert len(records) == len(paths)
    assert all(item["protocol_version"] == PROTOCOL_VERSION for item in records)
    assert all(item["name"] for item in records)
    assert any(isinstance(item["capabilities"], list) and item["capabilities"] for item in records)
