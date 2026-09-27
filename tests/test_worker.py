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
