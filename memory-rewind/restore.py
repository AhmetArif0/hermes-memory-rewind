"""Restore tracked files from history back into HERMES_HOME (user-initiated only)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .gitstore import GitStore
from .tracking import (
    MEMORY_FILES, SOUL_FILE, SKILLS_ROOT, TrackingOptions, collect_tracked_files,
    has_symlinked_component, is_trackable_path, is_trackable_target,
)

ALIASES = {"memory": MEMORY_FILES[0], "user": MEMORY_FILES[1], "soul": SOUL_FILE}


class RestoreError(ValueError):
    """The requested restore is not allowed or not possible."""


@dataclass
class RestorePlan:
    rev: str
    target: str
    write: dict[str, str] = field(default_factory=dict)  # path -> git mode
    delete: list[str] = field(default_factory=list)

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
    return plan


def apply_restore(store: GitStore, home: Path, plan: RestorePlan) -> None:
    for path in [*plan.write, *plan.delete]:
        if has_symlinked_component(home, path):
            raise RestoreError(f"refusing to write through a symlink: {path}")
    for path, mode in plan.write.items():
        _atomic_write(home / path, store.read_blob(plan.rev, path), executable=(mode == "100755"))
    for path in plan.delete:
        try:
            (home / path).unlink()
        except FileNotFoundError:
            pass
        _prune_empty_parents(home, path)


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
