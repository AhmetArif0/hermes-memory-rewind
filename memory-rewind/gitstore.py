"""Bare-git history store under the plugin's data directory.

Every git call is isolated from the user's git setup (global/system config, hooks,
signing, inherited GIT_* routing), the same way Hermes isolates its own shadow
checkpoints. Snapshots stage into a private temporary index and publish with a
compare-and-swap ``update-ref``, so concurrent Hermes processes sharing a profile
never corrupt each other and never need a lock file.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable

REF = "refs/heads/main"
_ZERO_OID = "0" * 40
_GIT_TIMEOUT_SECONDS = 60
_CAS_ATTEMPTS = 8
_AUTHOR_NAME = "memory-rewind"
_AUTHOR_EMAIL = "memory-rewind@localhost"
_GC_EVERY_N_COMMITS = 50

# Routing variables a parent process (or a user's shell) may have exported; any of them
# would redirect our git calls into someone else's repository or object store.
_STRIPPED_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES",
    "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_PAGER", "PAGER", "GIT_EDITOR", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_TRACE", "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS",
)


class GitUnavailable(RuntimeError):
    """git is not installed or not runnable."""


class GitError(RuntimeError):
    """A git command failed."""


class UnknownRevision(GitError):
    """A user-supplied version id is malformed or not in this history (safe to show the user)."""


def find_git() -> str | None:
    return shutil.which("git")


@dataclass
class Commit:
    sha: str
    timestamp: int
    subject: str
    body: str = ""
    changes: list[tuple[str, str]] = field(default_factory=list)  # (status, path)


class GitStore:
    def __init__(self, home: Path, data_dir: Path, git: str | None = None):
        self.home = Path(home)
        self.data_dir = Path(data_dir)
        self.repo = self.data_dir / "history.git"
        self.cache_index = self.data_dir / "index"
        self.git = git or find_git()
        if not self.git:
            raise GitUnavailable("git was not found on PATH")

    # ── plumbing ────────────────────────────────────────────────────────────────

    def _env(self, index: Path | None = None, with_worktree: bool = True,
             with_git_dir: bool = True) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_LITERAL_PATHSPECS"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = _AUTHOR_NAME
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = _AUTHOR_EMAIL
        if with_git_dir:
            env["GIT_DIR"] = str(self.repo)
        if with_worktree:
            env["GIT_WORK_TREE"] = str(self.home)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        return env

    def _run(self, args: list[str], *, index: Path | None = None, stdin: bytes | None = None,
             check: bool = True, with_worktree: bool = True, with_git_dir: bool = True,
             cwd: Path | None = None) -> subprocess.CompletedProcess:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                [self.git, *args],
                input=stdin,
                capture_output=True,
                env=self._env(index, with_worktree, with_git_dir),
                cwd=str(cwd or (self.home if with_worktree else self.data_dir)),
                timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
                **kwargs,
            )
        except FileNotFoundError as exc:
            raise GitUnavailable(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out after {_GIT_TIMEOUT_SECONDS}s") from exc
        if check and proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
            raise GitError(f"git {args[0]} failed: {detail}")
        return proc

    def _out(self, args: list[str], **kwargs) -> str:
        return self._run(args, **kwargs).stdout.decode("utf-8", "replace").strip()

    # ── repository lifecycle ────────────────────────────────────────────────────

    def exists(self) -> bool:
        return (self.repo / "HEAD").is_file()

    def ensure_repo(self) -> None:
        if self.exists():
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # `git init --bare` rejects GIT_WORK_TREE and must not inherit GIT_DIR.
        self._run(["init", "--quiet", "--bare", str(self.repo)], with_worktree=False,
                  with_git_dir=False)
        # HEAD must name the ref snapshots publish to, whatever init.defaultBranch says.
        self._run(["symbolic-ref", "HEAD", REF], with_worktree=False)
        for key, value in (("commit.gpgsign", "false"), ("core.hooksPath", os.devnull),
                           ("gc.auto", "256"), ("gc.autoDetach", "false"),
                           ("core.autocrlf", "false")):
            self._run(["config", "--local", key, value], with_worktree=False)

    def destroy(self) -> None:
        """Delete the history repository and index cache (privacy: `forget`)."""
        if self.repo.exists():
            _force_rmtree(self.repo)
        for leftover in (self.cache_index, *self.data_dir.glob("index.tmp.*")):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass

    # ── queries ─────────────────────────────────────────────────────────────────

    def head(self) -> str | None:
        if not self.exists():
            return None
        proc = self._run(["rev-parse", "--verify", "--quiet", f"{REF}^{{commit}}"],
                         check=False, with_worktree=False)
        out = proc.stdout.decode().strip()
        return out if proc.returncode == 0 and out else None

    def resolve(self, rev: str) -> str:
        """Resolve a user-supplied revision to a full commit id inside this history."""
        if not rev or rev.startswith("-") or any(c.isspace() for c in rev):
            raise UnknownRevision(f"invalid revision: {rev!r}")
        proc = self._run(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                         check=False, with_worktree=False)
        out = proc.stdout.decode().strip()
        if proc.returncode != 0 or not out:
            raise UnknownRevision(f"unknown revision: {rev}")
        return out

    def commit_count(self) -> int:
        if not self.head():
            return 0
        return int(self._out(["rev-list", "--count", REF], with_worktree=False) or 0)

    def log(self, paths: Iterable[str] = (), limit: int = 20, rev: str = REF) -> list[Commit]:
        """Versions reachable from *rev* (default: the newest), newest first."""
        if not self.head():
            return []
        sep, end = "\x1f", "\x1e"
        args = ["log", f"--max-count={max(1, limit)}", "--name-status", "--no-renames",
                f"--format={end}%H{sep}%ct{sep}%s{sep}%b{sep}", rev, "--", *paths]
        raw = self._run(args, with_worktree=False).stdout.decode("utf-8", "replace")
        commits: list[Commit] = []
        for chunk in raw.split(end):
            if not chunk.strip():
                continue
            sha, ts, subject, body, rest = chunk.split(sep, 4)
            changes = []
            for line in rest.strip().splitlines():
                status, _, path = line.partition("\t")
                if path:
                    changes.append((status.strip(), path))
            commits.append(Commit(sha.strip(), int(ts), subject, body.strip(), changes))
        return commits

    def ls_tree(self, rev: str, target: str) -> dict[str, str]:
        """Map path -> git mode for blobs at *rev* under *target* (file or directory)."""
        raw = self._run(["ls-tree", "-r", "-z", "--full-tree", rev, "--", target],
                        with_worktree=False).stdout.decode("utf-8", "replace")
        entries: dict[str, str] = {}
        for record in raw.split("\0"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            mode, kind, _oid = meta.split()
            if kind == "blob":
                entries[path] = mode
        return entries

    def read_blob(self, rev: str, path: str) -> bytes:
        return self._run(["cat-file", "blob", f"{rev}:{path}"], with_worktree=False).stdout

    def changes(self, rev: str, paths: Iterable[str] = ()) -> str:
        """Patch of what version *rev* changed against its parent (a first version: everything added)."""
        args = ["diff-tree", "-p", "-r", "--root", "--no-commit-id", "--no-color", "--no-ext-diff",
                "--no-renames", rev, "--", *paths]
        return self._run(args, with_worktree=False).stdout.decode("utf-8", "replace")

    def diff(self, old: str, new: str, paths: Iterable[str] = (), stat_only: bool = False) -> str:
        args = ["diff", "--no-color", "--no-ext-diff", "--no-renames"]
        if stat_only:
            args.append("--stat")
        args += [old, new, "--", *paths]
        return self._run(args, with_worktree=False).stdout.decode("utf-8", "replace")

    # ── snapshot ────────────────────────────────────────────────────────────────

    def snapshot(self, files: list[str], subject: str, body: str = "") -> str | None:
        """Record *files* (relative to home) as the new state. Returns the new commit id,
        or None when nothing changed since the current head."""
        self.ensure_repo()
        for _ in range(_CAS_ATTEMPTS):
            old = self.head()
            tmp_index = self.data_dir / f"index.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}"
            try:
                if self.cache_index.is_file():
                    # copy2 keeps the index file's mtime. git trusts a cached stat entry only when
                    # the index is newer than the file ("racy git"); a fresh mtime here would make
                    # a same-size edit in the same second as the last snapshot look unchanged.
                    shutil.copy2(self.cache_index, tmp_index)
                tree = self._stage(tmp_index, files)
                if old is not None and tree == self._out(["rev-parse", f"{old}^{{tree}}"],
                                                          with_worktree=False):
                    self._promote_index(tmp_index)
                    return None
                message = subject if not body else f"{subject}\n\n{body}"
                commit_args = ["commit-tree", tree, "-m", message]
                if old is not None:
                    commit_args[2:2] = ["-p", old]
                new = self._out(commit_args, with_worktree=False)
                cas = self._run(["update-ref", "-m", "memory-rewind snapshot", REF, new, old or _ZERO_OID],
                                check=False, with_worktree=False)
                if cas.returncode != 0:
                    continue  # another process moved the head first; restage against it
                self._promote_index(tmp_index)
                self._maybe_gc()
                return new
            finally:
                try:
                    tmp_index.unlink()
                except FileNotFoundError:
                    pass
        raise GitError("could not publish snapshot: history head kept moving")

    def _stage(self, index: Path, files: list[str]) -> str:
        wanted = set(files)
        present = self._run(["ls-files", "-z"], index=index).stdout.decode("utf-8", "replace")
        stale = [p for p in present.split("\0") if p and p not in wanted]
        if stale:
            self._run(["update-index", "--force-remove", "-z", "--stdin"], index=index,
                      stdin=_nul_join(stale))
        if files:
            self._run(["update-index", "--add", "--remove", "-z", "--stdin"], index=index,
                      stdin=_nul_join(files))
        return self._out(["write-tree"], index=index)

    def _promote_index(self, tmp_index: Path) -> None:
        """Keep the freshest stat cache for the next snapshot (best effort; never required).
        The copy keeps git's write time of the index (see snapshot)."""
        try:
            fd, staged = tempfile.mkstemp(prefix="index.tmp.promote.", dir=self.data_dir)
            os.close(fd)
            shutil.copy2(tmp_index, staged)
            os.replace(staged, self.cache_index)
        except OSError:
            pass

    def _maybe_gc(self) -> None:
        try:
            if self.commit_count() % _GC_EVERY_N_COMMITS == 0:
                # Never leave a detached background gc process behind.
                self._run(["-c", "gc.autoDetach=false", "gc", "--auto", "--quiet"],
                          check=False, with_worktree=False)
        except GitError:
            pass


def _force_rmtree(path: Path) -> None:
    """rmtree that also removes git's read-only object files (Windows refuses otherwise)."""
    def _retry_writable(func, target, _exc_info):
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda func, target, _exc: _retry_writable(func, target, None))
    else:
        shutil.rmtree(path, onerror=_retry_writable)


def _nul_join(paths: Iterable[str]) -> bytes:
    return b"".join(p.encode("utf-8") + b"\0" for p in paths)
