"""memory-rewind: automatic version history for the agent's own memory, skills and SOUL.md."""

from __future__ import annotations

import logging
from pathlib import Path

from .gitstore import GitError, GitUnavailable
from .tracking import DEFAULT_MAX_FILE_KB, TrackingOptions
from .worker import WORKER, take_snapshot

logger = logging.getLogger("memory_rewind")

PLUGIN_NAME = "memory-rewind"

# Tools that edit the tracked files directly.
_KNOWLEDGE_TOOLS = frozenset({"memory", "skill_manage"})
# File tools only matter when they touch a tracked path; a false positive just costs a no-op snapshot.
_FILE_TOOLS = frozenset({"write_file", "patch"})
_TRACKED_MARKERS = ("MEMORY.md", "USER.md", "SOUL.md", "skills")


def resolve_home() -> Path:
    """Active profile's HERMES_HOME, resolved in the caller's context (multiplex-safe)."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def resolve_data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return Path(plugin_data_dir(PLUGIN_NAME))


def read_options(ctx) -> TrackingOptions:
    def get(key, default):
        try:
            value = ctx.get_config(key, default)
        except Exception:
            return default
        return default if value is None else value

    try:
        max_kb = int(get("max_file_kb", DEFAULT_MAX_FILE_KB))
    except (TypeError, ValueError):
        max_kb = DEFAULT_MAX_FILE_KB
    return TrackingOptions(
        track_skills=bool(get("track_skills", True)),
        track_soul=bool(get("track_soul", True)),
        max_file_kb=max(1, max_kb),
    )


def _enabled(ctx) -> bool:
    try:
        return bool(ctx.get_config("enabled", True))
    except Exception:
        return True


def _touches_tracked_path(args) -> bool:
    if not isinstance(args, dict):
        return False
    text = " ".join(str(args.get(key) or "") for key in ("path", "patch"))
    return any(marker in text for marker in _TRACKED_MARKERS)


def register(ctx) -> None:
    def on_session_start(session_id: str = "", **_kwargs) -> None:
        # Baseline before the session can change anything; ~35 ms once the history exists.
        if not _enabled(ctx):
            return
        try:
            take_snapshot(resolve_home(), resolve_data_dir(), read_options(ctx),
                          ["session start"], [session_id])
        except GitUnavailable as exc:
            WORKER._warn_once("git-missing", "memory-rewind: git is unavailable, history paused (%s)", exc)
        except (GitError, OSError) as exc:
            logger.warning("memory-rewind: session-start snapshot failed: %s", exc)

    def post_tool_call(tool_name: str = "", args=None, session_id: str = "", **_kwargs) -> None:
        if tool_name in _KNOWLEDGE_TOOLS:
            reason = f"after {tool_name}"
        elif tool_name in _FILE_TOOLS and _touches_tracked_path(args):
            reason = f"after {tool_name}"
        else:
            return
        if _enabled(ctx):
            WORKER.submit(resolve_home(), resolve_data_dir(), read_options(ctx), reason, session_id)

    def on_session_end(session_id: str = "", **_kwargs) -> None:
        # Catches writes that bypass tools: curator runs, hub installs, terminal edits.
        if _enabled(ctx):
            WORKER.submit(resolve_home(), resolve_data_dir(), read_options(ctx), "turn end", session_id)

    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)

    from .cli import build_parser, run_cli

    ctx.register_cli_command(
        name=PLUGIN_NAME,
        help="Browse, diff and restore the history of memory, skills and SOUL.md",
        description=(
            "memory-rewind keeps an automatic version history of memories/MEMORY.md, "
            "memories/USER.md, SOUL.md and skills/ for the active profile."
        ),
        setup_fn=build_parser,
        handler_fn=lambda args: run_cli(args, ctx),
    )
