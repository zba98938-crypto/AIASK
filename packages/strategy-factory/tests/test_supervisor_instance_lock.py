"""Exercise competing supervisors with harmless subprocesses in hermetic homes."""

import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/factories/run_three_factories.py"


def load_supervisor():
    spec = importlib.util.spec_from_file_location("instance_lock_supervisor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def supervisor_command(log_dir, *, dry_run=False):
    # The real supervisor lifecycle runs, but only a Python sleep child may
    # launch. No factory imports, network access or database connections occur.
    code = f"""
import asyncio, importlib.util, sys
spec = importlib.util.spec_from_file_location('lock_subprocess_supervisor', {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._build_specs = lambda args, env: [module.FactorySpec('synthetic', (sys.executable, '-c', 'import time; time.sleep(60)'), 'synthetic.log')]
args = module.parse_args(['--python', sys.executable, '--log-dir', {str(log_dir)!r}, '--no-incubation', '--heartbeat-interval', '0'] + {['--dry-run'] if dry_run else []!r})
raise SystemExit(asyncio.run(module._run(args)))
"""
    return [sys.executable, "-u", "-c", code]


def spawn_supervisor(tmp_path, name, runtime):
    env = os.environ.copy()
    env.update(AIASK_RUNTIME_HOME=str(runtime), AIASK_LOAD_PROJECT_ENV="0")
    stdout = (tmp_path / f"{name}.stdout").open("w")
    stderr = (tmp_path / f"{name}.stderr").open("w")
    try:
        process = subprocess.Popen(
            supervisor_command(tmp_path / name), env=env, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
    finally:
        stdout.close()
        stderr.close()
    return process


def wait_for_child(process, log_dir):
    deadline = time.monotonic() + 8
    records = log_dir / "execution_records.jsonl"
    while time.monotonic() < deadline:
        if records.exists():
            for line in records.read_text().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row["event_type"] == "factory_start":
                    return row["pid"]
        if process.poll() is not None:
            pytest.fail(f"synthetic supervisor exited before child startup: {process.returncode}")
        time.sleep(0.02)
    pytest.fail("synthetic supervisor did not start within 8 seconds")


def stop_group(process):
    # Reap even if the assertion fails before the synthetic supervisor stops.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="fcntl instance lock is POSIX")
def test_competing_supervisor_is_rejected_then_lock_releases(tmp_path):
    runtime = tmp_path / "runtime"
    lock_path = runtime / "run/four_runtime_supervisor.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": "stale metadata"}))
    first = spawn_supervisor(tmp_path, "first", runtime)
    second = third = None
    try:
        wait_for_child(first, tmp_path / "first")
        owner_bytes = lock_path.read_bytes()
        env = os.environ.copy()
        env.update(AIASK_RUNTIME_HOME=str(runtime), AIASK_LOAD_PROJECT_ENV="0")
        dry = subprocess.run(
            supervisor_command(tmp_path / "dry", dry_run=True), env=env,
            capture_output=True, text=True, timeout=8,
        )
        assert dry.returncode == 0, dry.stderr
        assert lock_path.read_bytes() == owner_bytes
        assert not (tmp_path / "dry").exists()
        second = spawn_supervisor(tmp_path, "second", runtime)
        assert second.wait(timeout=3) == 2
        assert "supervisor_already_running" in (tmp_path / "second.stderr").read_text()
        assert not (tmp_path / "second" / "execution_records.jsonl").exists()
        first.terminate()
        assert first.wait(timeout=5) == 0
        third = spawn_supervisor(tmp_path, "third", runtime)
        wait_for_child(third, tmp_path / "third")
    finally:
        for process in (first, second, third):
            if process is not None:
                stop_group(process)


@pytest.mark.skipif(os.name != "posix", reason="fcntl instance lock is POSIX")
def test_separate_runtime_homes_can_each_have_a_supervisor(tmp_path):
    first = spawn_supervisor(tmp_path, "first", tmp_path / "runtime-a")
    second = None
    try:
        first_child = wait_for_child(first, tmp_path / "first")
        second = spawn_supervisor(tmp_path, "second", tmp_path / "runtime-b")
        second_child = wait_for_child(second, tmp_path / "second")
        assert first_child != second_child
        assert first.poll() is None and second.poll() is None
    finally:
        for process in (first, second):
            if process is not None:
                stop_group(process)


@pytest.mark.skipif(os.name != "posix", reason="fcntl instance lock is POSIX")
def test_orphan_factory_retains_lock_after_supervisor_killed(tmp_path):
    runtime = tmp_path / "runtime"
    first = spawn_supervisor(tmp_path, "first", runtime)
    second = third = None
    try:
        child_pid = wait_for_child(first, tmp_path / "first")
        first.kill()
        first.wait(timeout=5)
        os.kill(child_pid, 0)
        second = spawn_supervisor(tmp_path, "second", runtime)
        assert second.wait(timeout=3) == 2
        assert "supervisor_already_running" in (tmp_path / "second.stderr").read_text()
        stop_group(first)
        # Waiting on a separately opened flock verifies release without relying
        # on PID liveness (a reaped or zombie orphan may still have a PID).
        import fcntl

        with (runtime / "run/four_runtime_supervisor.lock").open("a") as lock:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        third = spawn_supervisor(tmp_path, "third", runtime)
        wait_for_child(third, tmp_path / "third")
    finally:
        for process in (first, second, third):
            if process is not None:
                stop_group(process)


def test_dry_run_does_not_create_lock_or_business_records(tmp_path):
    runtime = tmp_path / "runtime"
    env = os.environ.copy()
    env.update(AIASK_RUNTIME_HOME=str(runtime), AIASK_LOAD_PROJECT_ENV="0")
    result = subprocess.run(
        supervisor_command(tmp_path / "logs", dry_run=True), env=env,
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0, result.stderr
    assert not runtime.exists()
    assert not (tmp_path / "logs").exists()


@pytest.mark.asyncio
async def test_expected_interval_timeout_does_not_warn(caplog):
    module = load_supervisor()
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING):
        await module._sleep_or_stop(stop, 0.01)
    assert not stop.is_set()
    assert not caplog.records
