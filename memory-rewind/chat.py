"""`/memory-history`: the history, read-only, from any chat surface (CLI, TUI/Desktop, gateway platforms).

Restoring stays a host command (`hermes memory-rewind restore`): a chat message never rewrites
the agent's knowledge files, and viewing never records a version. Output follows Hermes' own chat
conventions: plain text, within the 3,000-character budget its `/skills diff` uses in chat. A reply
may land in a group chat, so it never carries host paths or raw git errors.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .cli import format_log
from .gitstore import GitError, GitStore, GitUnavailable, UnknownRevision
from .restore import ALIASES, RestoreError, normalize_target
from .tracking import MEMORY_FILES, SKILLS_ROOT, SOUL_FILE, TrackingOptions
from .worker import WORKER

logger = logging.getLogger("memory_rewind")

COMMAND = "memory-history"
CHAT_LIMIT = 3000
RECENT_VERSIONS = 10
FILES_PER_VERSION = 3
_FLUSH_SECONDS = 2.0  # let a snapshot queued at the end of the last turn land first

USAGE = f"""\
/{COMMAND}: versions of memory, skills and SOUL.md (read-only)
  /{COMMAND}                              the last {RECENT_VERSIONS} versions
  /{COMMAND} memory | user | soul         versions of MEMORY.md, USER.md or SOUL.md
  /{COMMAND} skills/<category>/<skill>    versions of one skill
  /{COMMAND} <version> [target]           what that version changed
Restoring is a host command: hermes memory-rewind restore <version> <target>"""


def handle_slash(raw_args: str, ctx) -> str:
    """Slash-command entry point; HERMES_HOME resolves in the caller's (profile-scoped) context."""
    from . import read_options, resolve_data_dir, resolve_home
    return render_history(raw_args, resolve_home(), resolve_data_dir(), read_options(ctx))


def render_history(raw_args: str, home: Path, data_dir: Path, options: TrackingOptions) -> str:
    args = (raw_args or "").split()
    if len(args) > 2 or (args and args[0].lower() in ("help", "-h", "--help")):
        return USAGE
    if len(args) == 2 and not _is_target(args[1]):
        return f"{args[1]} is not a memory-rewind target.\n\n{USAGE}"
    try:
        store = GitStore(home, data_dir)
    except GitUnavailable:
        return "memory-rewind needs git on PATH on the Hermes host."
    WORKER.flush(timeout=_FLUSH_SECONDS)
    try:
        if not store.head():
            return "No history yet. It starts with the next session."
        if not args or _is_target(args[0]):
            if len(args) > 1:
                return USAGE
            paths = [normalize_target(args[0], home, options)] if args else []
            commits = store.log(paths, limit=RECENT_VERSIONS)
            if not commits:
                return "No versions touch that path yet."
            return _clip(format_log(commits, FILES_PER_VERSION), "hermes memory-rewind log")
        rev = store.resolve(args[0])
        paths = [normalize_target(args[1], home, options)] if len(args) > 1 else []
        return _version(store, rev, paths)
    except (UnknownRevision, RestoreError) as exc:  # messages built from the user's own input
        return f"memory-rewind: {exc}\n\n{USAGE}"
    except GitError as exc:
        logger.warning("memory-rewind: /%s failed: %s", COMMAND, exc)
        return "memory-rewind could not read the history; the Hermes log has the details."


def _is_target(arg: str) -> bool:
    """A tracked-file or skill target rather than a version id (never an absolute path)."""
    text = arg.replace("\\", "/")
    return (text.lower() in ALIASES or text == SOUL_FILE or text in MEMORY_FILES
            or text.split("/")[0] == SKILLS_ROOT)


def _version(store: GitStore, rev: str, paths: list[str]) -> str:
    commit = store.log(limit=1, rev=rev)[0]
    short = rev[:10]
    only_file = commit.changes[0][1] if len(commit.changes) == 1 else "<target>"
    target = paths[0] if paths else only_file
    try:
        store.resolve(f"{rev}~1")
    except UnknownRevision:  # the first version: nothing came before it
        hints, full = [], f"hermes memory-rewind show {short} <file>"
    else:
        full = " ".join(["hermes memory-rewind diff", f"{short}~1", *paths, "--to", short])
        hints = [f"State before this version: hermes memory-rewind restore {short}~1 {target}"]
    head = "\n".join([format_log([commit], max_files=0), *hints])
    patch = store.changes(rev, paths).rstrip("\n")
    if not patch.strip():
        return f"{head}\n\nThis version did not change {target}."
    return f"{head}\n\n" + _clip(patch, full, budget=CHAT_LIMIT - len(head) - 2)


def _clip(text: str, full_output: str, budget: int = CHAT_LIMIT) -> str:
    """*text* cut at a line boundary to fit *budget* characters, with a pointer to the full output."""
    if len(text) <= budget:
        return text
    note = f"\n… (truncated; full output on the host: {full_output})"
    room = max(0, budget - len(note))
    cut = text.rfind("\n", 0, room)
    return text[:cut if cut > 0 else room] + note
