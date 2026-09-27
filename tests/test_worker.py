from __future__ import annotations

import os
import subprocess
import sys
import textwrap

from conftest import PLUGIN_DIR


def test_queued_snapshot_lands_before_process_exit(home, data_dir, store):
    """A one-shot process that queues a snapshot and exits immediately must still record it."""
    code = textwrap.dedent(f"""
        import importlib.util, sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "mrw", {str(PLUGIN_DIR / "__init__.py")!r},
            submodule_search_locations=[{str(PLUGIN_DIR)!r}])
        mrw = importlib.util.module_from_spec(spec); sys.modules["mrw"] = mrw; spec.loader.exec_module(mrw)
        from mrw.worker import WORKER
        from mrw.tracking import TrackingOptions
        WORKER.submit(Path({str(home)!r}), Path({str(data_dir)!r}), TrackingOptions(), "turn end", "s1")
        # exit immediately, no explicit flush
    """)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert store.head() is not None, "the queued snapshot was lost at exit"
    assert store.log(limit=1)[0].subject == "turn end"


def test_worker_coalesces_requests_for_one_profile(home, data_dir, store, options):
    from memory_rewind.worker import SnapshotWorker
    worker = SnapshotWorker()
    for i in range(20):
        worker.submit(home, data_dir, options, f"after memory {i % 2}", f"s{i % 3}")
    assert worker.flush(timeout=60)
    assert 1 <= store.commit_count() <= 2  # at most the in-flight one plus one coalesced batch


def test_worker_records_provenance(home, data_dir, store, options):
    from memory_rewind.provenance import parse_body
    from memory_rewind.worker import SnapshotWorker
    worker = SnapshotWorker()
    worker.submit(home, data_dir, options, "after memory", "s1", platform="telegram",
                  call="memory: add (user)")
    assert worker.flush(timeout=60)
    newest = store.log(limit=1)[0]
    assert newest.subject == "after memory"
    origin = parse_body(newest.body)
    assert origin.calls == ["memory: add (user)"]
    assert origin.sessions == [("s1", "telegram")]


def test_request_merges_sessions_and_bounds_calls(home, data_dir, options):
    from memory_rewind.provenance import MAX_CALLS
    from memory_rewind.worker import SnapshotRequest
    request = SnapshotRequest(home, data_dir, options)
    request.add_session("s1", "")
    request.add_session("s2", "cli")
    request.add_session("s1", "telegram")  # learned later
    request.add_session("s1", "")  # an unknown platform never erases a known one
    assert request.sessions == {"s1": "telegram", "s2": "cli"}
    for i in range(MAX_CALLS + 5):
        request.add_call(f"memory: add ({i})")
    request.add_call("")
    assert len(request.calls) == MAX_CALLS and request.total_calls == MAX_CALLS + 5
