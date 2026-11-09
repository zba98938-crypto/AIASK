"""Universe dispatch must not silently discard codes at candidate target caps."""
import asyncio
from pathlib import Path
import sys

import pytest


@pytest.mark.parametrize('requested_shard_size', [None, 20, 200])
def test_two_hundred_codes_all_reach_scheduler(requested_shard_size):
    scripts = str(Path(__file__).resolve().parents[3] / 'scripts/factories')
    sys.path.insert(0, scripts)
    try:
        from run_strategy_factory import StrategyFactoryRunner
    finally:
        sys.path.remove(scripts)
    from strategy_factory.domain.targets import _normalize_target_codes

    class Scheduler:
        def __init__(self):
            self.received = []

        async def dispatch_run(self, db, *, execution_mode, target_codes):
            # Exercise the real scheduler's target normalizer, not an unlimited mock.
            self.received.append(_normalize_target_codes(target_codes, limit=64))
            return {'status': 'queued'}

    codes = [f'{i:06d}' for i in range(1, 201)]
    scheduler = Scheduler()
    runner = StrategyFactoryRunner(target_codes=codes, dispatch_run=True, dispatch_shard_size=requested_shard_size)
    result = asyncio.run(runner._execute_dispatch_run_batch(scheduler, {}))
    assert [code for batch in scheduler.received for code in batch] == codes
    assert max(map(len, scheduler.received)) <= 40
    assert result['summary']['dispatch_count'] == (10 if requested_shard_size == 20 else 5)


def test_dispatch_waits_for_each_shard_and_stops_on_failed_shard():
    scripts = str(Path(__file__).resolve().parents[3] / 'scripts/factories')
    sys.path.insert(0, scripts)
    try:
        from run_strategy_factory import StrategyFactoryRunner
    finally:
        sys.path.remove(scripts)

    class Scheduler:
        def __init__(self):
            self.active = None
            self.calls = []

        async def dispatch_run(self, db, *, execution_mode, target_codes):
            assert self.active is None, 'must not enqueue while previous shard pending'
            self.active = str(len(self.calls) + 1)
            self.calls.append(target_codes)
            return {'status': 'queued', 'dispatch_id': self.active}

        async def get_dispatch_status(self, dispatch_id):
            assert dispatch_id == self.active
            self.active = None
            return {'status': 'failed' if dispatch_id == '2' else 'success', 'dispatch_id': dispatch_id}

    scheduler = Scheduler()
    runner = StrategyFactoryRunner(target_codes=[f'{i:06d}' for i in range(200)], dispatch_run=True)
    result = asyncio.run(runner._execute_dispatch_run_batch(scheduler, {}))
    assert len(scheduler.calls) == 2
    assert result['status'] == 'failed'
    assert runner._pending_batch_dispatch is None


def test_unknown_pending_status_never_enqueues_new_generation():
    scripts = str(Path(__file__).resolve().parents[3] / 'scripts/factories')
    sys.path.insert(0, scripts)
    try:
        from run_strategy_factory import StrategyFactoryRunner
    finally:
        sys.path.remove(scripts)

    class Scheduler:
        async def dispatch_run(self, *args, **kwargs):
            raise AssertionError('pending work must be resolved first')

        async def get_dispatch_status(self, dispatch_id):
            return None

    runner = StrategyFactoryRunner(target_codes=['000001'], dispatch_run=True)
    runner._pending_batch_dispatch = 'pending'
    with pytest.raises(RuntimeError, match='refusing to enqueue'):
        asyncio.run(runner._execute_dispatch_run_batch(Scheduler(), {}))
    assert runner._pending_batch_dispatch == 'pending'
