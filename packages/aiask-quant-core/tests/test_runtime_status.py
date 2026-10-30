from datetime import datetime, timedelta, timezone


def test_effective_status_distinguishes_live_dead_and_stale():
    from aiask_quant_core.runtime_status import effective_status

    live = effective_status("running", pid=__import__("os").getpid())
    assert live == {"effective_status": "running", "last_reported_status": "running"}

    dead = effective_status("running", pid=2**31 - 1)
    assert dead == {"effective_status": "dead", "last_reported_status": "running"}

    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    stale = effective_status(
        "running",
        pid=__import__("os").getpid(),
        heartbeat_at=(now - timedelta(seconds=181)).isoformat(),
        heartbeat_period_seconds=60,
        now=now,
    )
    assert stale == {"effective_status": "stale", "last_reported_status": "running"}
