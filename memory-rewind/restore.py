"""Restore tracked files from history back into HERMES_HOME (user-initiated only).

A restore applies exactly the plan it showed. It takes the memory tool's own lock on a
memory file while it checks and writes it, so it never interleaves with a live agent's
memory write, and it refuses when a file changed after the plan was made (an agent kept
working while the user read the plan): nothing written since then is silently replaced.
"""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

from .gitstore import GitStore
from .tracking import (
    MEMORY_FILES, SOUL_FILE, SKILLS_ROOT, TrackingOptions, collect_tracked_files,
    has_symlinked_component, is_trackable_path, is_trackable_target,
)

ALIASES = {"memory": MEMORY_FILES[0], "user": MEMORY_FILES[1], "soul": SOUL_FILE}
# Hermes' memory store separates entries with this and drops empty ones (MemoryStore._parse_entries).
ENTRY_DELIMITER = "\n§\n"
LOCK_WAIT_SECONDS = 10.0
_UNREADABLE = "unreadable"
# "Held by someone else" from a non-blocking flock (POSIX) or msvcrt LK_NBLCK (Windows).
_LOCK_BUSY = frozenset(filter(None, (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES,
                                     getattr(errno, "EDEADLK", None), getattr(errno, "EDEADLOCK", None))))


class RestoreError(ValueError):
    """The requested restore is not allowed or not possible."""


@dataclass
class RestorePlan:
    rev: str
    target: str
    write: dict[str, str] = field(default_factory=dict)  # path -> git mode
    delete: list[str] = field(default_factory=list)
    # path -> sha256 of the content the plan was made from (None: the file did not exist)
    seen: dict[str, str | None] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.write and not self.delete


def normalize_target(raw: str, home: Path, options: TrackingOptions) -> str:
    """Turn a user-supplied target into a tracked relative path, or raise RestoreError."""
    text = (raw or "").strip()
    if not text:
        raise RestoreError("a target is required (memory, user, soul, or a path under skills/)")
    if text.lower() in ALIASES:
        return ALIASES[text.lower()]
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        try:
            text = candidate.resolve(strict=False).relative_to(home.resolve()).as_posix()
        except ValueError:
            raise RestoreError(f"{raw} is outside the Hermes home ({home})") from None
    rel = PurePosixPath(text.replace("\\", "/")).as_posix().rstrip("/")
    if not is_trackable_target(rel, options):
        raise RestoreError(f"{raw} is not tracked by memory-rewind")
    return rel


def plan_restore(store: GitStore, home: Path, rev: str, target: str,
                 options: TrackingOptions) -> RestorePlan:
    commit = store.resolve(rev)
    at_rev = {p: m for p, m in store.ls_tree(commit, target).items() if _under(p, target)}
    if not at_rev and not _is_dir_target(target):
        raise RestoreError(f"{target} does not exist at {rev}")
    now = {p for p in collect_tracked_files(home, options) if _under(p, target)}
    plan = RestorePlan(rev=commit, target=target)
    for path, mode in sorted(at_rev.items()):
        if not is_trackable_path(path, options):
            continue  # never write something the tracking rules exclude today
        current = home / path
        if path in now and _same_content(store, commit, path, current, mode):
            continue
        plan.write[path] = mode
    plan.delete = sorted(p for p in now if p not in at_rev)
    if at_rev == {} and not plan.delete:
        raise RestoreError(f"nothing under {target} at {rev} and nothing to remove now")
    plan.seen = {path: _digest(home / path) for path in sorted(set(at_rev) | now)
                 if is_trackable_path(path, options)}
    return plan


def apply_restore(store: GitStore, home: Path, plan: RestorePlan) -> None:
    for path in [*plan.write, *plan.delete]:
        if has_symlinked_component(home, path):
            raise RestoreError(f"refusing to write through a symlink: {path}")
    with ExitStack() as locks:
        for path in sorted(p for p in plan.seen if p in MEMORY_FILES):
            locks.enter_context(_memory_file_lock(home / path))
        changed = [path for path, digest in plan.seen.items() if _digest(home / path) != digest]
        if changed:
            raise RestoreError(f"{changed[0]} changed after the restore plan was made; nothing was "
                               "restored. Run the command again to see the current plan.")
        for path, mode in plan.write.items():
            _atomic_write(home / path, store.read_blob(plan.rev, path), executable=(mode == "100755"))
        for path in plan.delete:
            try:
                (home / path).unlink()
            except FileNotFoundError:
                pass
            _prune_empty_parents(home, path)


def memory_entry_changes(current: bytes | None, restored: bytes | None) -> tuple[list[str], list[str]]:
    """(back, gone): memory entries the restore brings back, and entries it removes."""
    now, then = _entries(current), _entries(restored)
    return [e for e in then if e not in now], [e for e in now if e not in then]


def _entries(raw: bytes | None) -> list[str]:
    if not raw:
        return []
    text = raw.decode("utf-8", "replace")
    return list(dict.fromkeys(e for e in (x.strip() for x in text.split(ENTRY_DELIMITER)) if e))


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE


@contextmanager
def _memory_file_lock(path: Path):
    """The lock Hermes' memory tool holds while it re-reads and writes a memory file
    (MemoryStore._file_lock): an exclusive lock on ``<file>.lock`` beside it, flock on POSIX
    and a one-byte lock at offset 0 on Windows. Waits up to LOCK_WAIT_SECONDS."""
    if fcntl is None and msvcrt is None:
        yield
        return
    lock_path = path.with_name(path.name + ".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise RestoreError(f"cannot open {lock_path.name}: {exc.strerror or exc}") from exc
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while not _try_lock(fd, lock_path.name):
            if time.monotonic() >= deadline:
                raise RestoreError(f"{path.name} is being written by a running Hermes; nothing was "
                                   "restored. Try again in a moment.")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _try_lock(fd: int, name: str) -> bool:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in _LOCK_BUSY:
            return False
        raise RestoreError(f"cannot lock {name}: {exc.strerror or exc}") from exc
    return True


def _unlock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _under(path: str, target: str) -> bool:
    return path == target or path.startswith(target + "/")


def _is_dir_target(target: str) -> bool:
    return target == SKILLS_ROOT or (target.startswith(SKILLS_ROOT + "/") and target not in MEMORY_FILES)


def _same_content(store: GitStore, rev: str, path: str, current: Path, mode: str) -> bool:
    try:
        if current.read_bytes() != store.read_blob(rev, path):
            return False
    except OSError:
        return False
    if os.name == "nt":
        return True
    return bool(os.stat(current).st_mode & 0o111) == (mode == "100755")


def _atomic_write(dest: Path, data: bytes, executable: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".mrw", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o755 if executable else 0o644)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _prune_empty_parents(home: Path, path: str) -> None:
    """Remove directories emptied by a delete, stopping at skills/ itself."""
    stop = (home / SKILLS_ROOT).resolve()
    current = (home / path).parent
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            return
        if resolved == stop or stop not in resolved.parents:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
