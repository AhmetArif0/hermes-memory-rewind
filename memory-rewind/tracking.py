"""Which files under HERMES_HOME memory-rewind versions, and which it never touches.

Paths are returned relative to HERMES_HOME in POSIX form (the shape git stores).
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MEMORY_FILES = ("memories/MEMORY.md", "memories/USER.md")
SOUL_FILE = "SOUL.md"
SKILLS_ROOT = "skills"

# Hermes bookkeeping inside skills/: churns on every skill read, or is itself a backup.
_SKILLS_BOOKKEEPING_FILES = frozenset({".usage.json", ".curator_ledger.jsonl", ".bundled_manifest"})
_SKILLS_BOOKKEEPING_PREFIXES = (".curator_suppressed",)
# .hub holds the hub lock, audit log, index cache and quarantined skills; quarantined
# content must never become restorable.
_SKILLS_EXCLUDED_DIRS = frozenset({".curator_backups", ".hub"})

_NOISE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox",
})
_NOISE_FILES = frozenset({".DS_Store", "Thumbs.db"})

# Secret-shaped and database files are never versioned, wherever they sit.
_SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ecdsa*",
    "id_ed25519*", "*.keystore", "auth.json", "credentials*.json",
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3", "*.sqlite-*",
)

DEFAULT_MAX_FILE_KB = 1024


@dataclass(frozen=True)
class TrackingOptions:
    track_skills: bool = True
    track_soul: bool = True
    max_file_kb: int = DEFAULT_MAX_FILE_KB

    @property
    def max_file_bytes(self) -> int:
        return max(1, int(self.max_file_kb)) * 1024


def is_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _SECRET_PATTERNS)


def _excluded_under_skills(parts: tuple[str, ...]) -> bool:
    """True for Hermes bookkeeping paths directly under skills/ (parts exclude 'skills')."""
    if not parts:
        return False
    head = parts[0]
    if head in _SKILLS_EXCLUDED_DIRS:
        return True
    if len(parts) == 1 and (head in _SKILLS_BOOKKEEPING_FILES
                            or head.startswith(_SKILLS_BOOKKEEPING_PREFIXES)):
        return True
    return False


def is_trackable_path(rel: str, options: TrackingOptions) -> bool:
    """Name-level check (no filesystem access): may *rel* ever appear in history?"""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or not pure.parts or any(p in ("", ".", "..") for p in pure.parts):
        return False
    parts = pure.parts
    if any(p in _NOISE_DIRS for p in parts[:-1]) or parts[-1] in _NOISE_FILES:
        return False
    if any(is_secret_name(p) for p in parts):
        return False
    if rel in MEMORY_FILES:
        return True
    if rel == SOUL_FILE:
        return options.track_soul
    if parts[0] == SKILLS_ROOT and len(parts) > 1:
        return options.track_skills and not _excluded_under_skills(parts[1:])
    return False


def is_trackable_target(rel: str, options: TrackingOptions) -> bool:
    """Like is_trackable_path, but also accepts directories under skills/ (restore targets)."""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or not pure.parts or any(p in ("", ".", "..") for p in pure.parts):
        return False
    if rel == SKILLS_ROOT:
        return options.track_skills
    if pure.parts[0] == SKILLS_ROOT:
        if any(p in _NOISE_DIRS for p in pure.parts) or any(is_secret_name(p) for p in pure.parts):
            return False
        return options.track_skills and not _excluded_under_skills(pure.parts[1:])
    return is_trackable_path(rel, options)


def _regular_file_size(path: Path) -> int | None:
    """Size of a regular, non-symlink file; None for anything else."""
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return st.st_size


def collect_tracked_files(home: Path, options: TrackingOptions) -> list[str]:
    """Walk HERMES_HOME and return the sorted list of files to version right now."""
    limit = options.max_file_bytes
    found: list[str] = []

    candidates = list(MEMORY_FILES)
    if options.track_soul:
        candidates.append(SOUL_FILE)
    for rel in candidates:
        size = _regular_file_size(home / rel)
        if size is not None and size <= limit and not _has_symlinked_parent(home, rel):
            found.append(rel)

    skills = home / SKILLS_ROOT
    if options.track_skills and _is_real_dir(skills):
        for dirpath, dirnames, filenames in os.walk(skills, followlinks=False):
            current = Path(dirpath)
            rel_dir = current.relative_to(home).as_posix()
            # Prune: symlinked dirs, noise dirs, bookkeeping dirs, embedded git repositories.
            keep = []
            for name in dirnames:
                child = current / name
                child_rel = f"{rel_dir}/{name}"
                if name in _NOISE_DIRS or not _is_real_dir(child):
                    continue
                if not is_trackable_target(child_rel, options):
                    continue
                if (child / ".git").exists():
                    continue
                keep.append(name)
            dirnames[:] = sorted(keep)
            for name in filenames:
                rel = f"{rel_dir}/{name}"
                if not is_trackable_path(rel, options):
                    continue
                size = _regular_file_size(current / name)
                if size is None or size > limit:
                    continue
                found.append(rel)
    return sorted(found)


def _is_real_dir(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


def _has_symlinked_parent(home: Path, rel: str) -> bool:
    current = home
    for part in PurePosixPath(rel).parts[:-1]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except OSError:
            return False
    return False


def has_symlinked_component(home: Path, rel: str) -> bool:
    """True when any existing component of *rel* (parents or the leaf) is a symlink."""
    current = home
    for part in PurePosixPath(rel).parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return True
    return False
