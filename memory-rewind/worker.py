"""Per-process background snapshot queue.

Hook callbacks resolve HERMES_HOME and the data directory in the caller's context
(so multiplexed profiles stay separate) and hand plain paths to this worker; git
never runs on the agent loop except for the session-start baseline.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .gitstore import GitError, GitStore, GitUnavailable
from .provenance import MAX_CALLS, build_body
from .tracking import TrackingOptions, collect_tracked_files

logger = logging.getLogger("memory_rewind")


@dataclass
class SnapshotRequest:
    home: Path
    data_dir: Path
    options: TrackingOptions
    reasons: list[str] = field(default_factory=list)
    sessions: dict[str, str] = field(default_factory=dict)  # session id -> platform ("" if unknown)
    calls: list[str] = field(default_factory=list)  # first MAX_CALLS call summaries
    total_calls: int = 0

    def add_session(self, session_id: str, platform: str = "") -> None:
        if session_id:  # an unknown platform never erases a known one
            self.sessions[session_id] = platform or self.sessions.get(session_id, "")

    def add_call(self, call: str) -> None:
        if call:
            self.total_calls += 1
            if len(self.calls) < MAX_CALLS:
                self.calls.append(call)


def take_snapshot(home: Path, data_dir: Path, options: TrackingOptions, reasons: list[str],
                  sessions: dict[str, str] | None = None, calls: list[str] = (),
                  total_calls: int = 0) -> str | None:
    """Snapshot synchronously. Returns the new commit id, or None when nothing changed."""
    files = collect_tracked_files(home, options)
    unique = list(OrderedDict.fromkeys(r for r in reasons if r)) or ["snapshot"]
    subject = "; ".join(unique)[:200]
    body = build_body(list(calls), total_calls, dict(sessions or {}))
    return GitStore(home, data_dir).snapshot(files, subject, body)


class SnapshotWorker:
    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._pending: OrderedDict[str, SnapshotRequest] = OrderedDict()
        self._thread: threading.Thread | None = None
        self._warned: set[str] = set()
        self._busy = False

    def submit(self, home: Path, data_dir: Path, options: TrackingOptions, reason: str,
               session_id: str = "", platform: str = "", call: str = "") -> None:
        key = str(data_dir)
        with self._lock:
            request = self._pending.get(key)
            if request is None:
                request = self._pending[key] = SnapshotRequest(home, data_dir, options)
            request.options = options
            if reason not in request.reasons:
                request.reasons.append(reason)
            request.add_session(session_id, platform)
            request.add_call(call)
            self._ensure_thread()
            self._lock.notify()

    def flush(self, timeout: float = 30.0) -> bool:
        """Block until the queue is drained (tests, and the CLI before reading history)."""
        with self._lock:
            return self._lock.wait_for(lambda: not self._pending and not self._busy, timeout)

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="memory-rewind", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._lock:
                self._lock.wait_for(lambda: bool(self._pending))
                _key, request = self._pending.popitem(last=False)
                self._busy = True
            try:
                take_snapshot(request.home, request.data_dir, request.options, request.reasons,
                              request.sessions, request.calls, request.total_calls)
            except GitUnavailable as exc:
                self._warn_once("git-missing", "memory-rewind: git is unavailable, history paused (%s)", exc)
            except (GitError, OSError) as exc:
                self._warn_once(f"err:{type(exc).__name__}:{exc}", "memory-rewind: snapshot failed: %s", exc)
            except Exception:  # never let the worker thread die silently
                logger.exception("memory-rewind: unexpected snapshot failure")
            finally:
                with self._lock:
                    self._busy = False
                    self._lock.notify_all()

    def _warn_once(self, key: str, message: str, *args) -> None:
        if key in self._warned:
            logger.debug(message, *args)
            return
        self._warned.add(key)
        logger.warning(message, *args)


WORKER = SnapshotWorker()

# One-shot CLI runs exit right after the turn: give queued snapshots a bounded chance to land
# (atexit runs before daemon threads are stopped at interpreter shutdown).
_EXIT_FLUSH_SECONDS = 5.0
atexit.register(lambda: WORKER.flush(timeout=_EXIT_FLUSH_SECONDS))
