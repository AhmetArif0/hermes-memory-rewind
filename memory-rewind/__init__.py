"""memory-rewind: automatic version history for the agent's own memory, skills and SOUL.md."""

from __future__ import annotations

import logging
from pathlib import Path

from .gitstore import GitError, GitUnavailable
from .provenance import SessionPlatforms, describe_call
from .tracking import DEFAULT_MAX_FILE_KB, TrackingOptions
from .worker import WORKER, take_snapshot

logger = logging.getLogger("memory_rewind")

PLUGIN_NAME = "memory-rewind"
SESSIONS = SessionPlatforms()

# Tools that edit the tracked files directly.
_KNOWLEDGE_TOOLS = frozenset({"memory", "skill_manage"})
# File tools only matter when they touch a tracked path; a false positive just costs a no-op snapshot.
_FILE_TOOLS = frozenset({"write_file", "patch"})
_TRACKED_MARKERS = ("MEMORY.md", "USER.md", "SOUL.md", "skills")
# How long a baseline waits for queued snapshots; well under Hermes' 30 s hook timeout.
_BASELINE_FLUSH_SECONDS = 5.0


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


def _record_baseline(ctx, reason: str) -> None:
    """Record the current state now, naming no tool call and no session.

    The turn or session about to start has changed nothing yet, so whatever changed since
    the last version was done by something else (an approved staged write, a curator run,
    a Desktop edit, another session) and is credited to no one. Queued snapshots land
    first, so the calls they name keep their changes.
    """
    if not _enabled(ctx):
        return
    WORKER.flush(timeout=_BASELINE_FLUSH_SECONDS)
    try:
        take_snapshot(resolve_home(), resolve_data_dir(), read_options(ctx), [reason])
    except GitUnavailable as exc:
        WORKER._warn_once("git-missing", "memory-rewind: git is unavailable, history paused (%s)", exc)
    except (GitError, OSError) as exc:
        logger.warning("memory-rewind: %s snapshot failed: %s", reason, exc)


def register(ctx) -> None:
    def on_session_start(session_id: str = "", platform: str = "", **_kwargs) -> None:
        # Baseline before the session can change anything; ~40 ms once the history exists.
        SESSIONS.remember(session_id, platform)
        _record_baseline(ctx, "session start")

    def pre_llm_call(session_id: str = "", platform: str = "") -> None:
        # Baseline at every turn start, so changes made between turns never land in a version
        # that names this turn's calls or session. Only these two fields are declared, so
        # Hermes never passes the user message or the conversation; returns nothing to inject.
        SESSIONS.remember(session_id, platform)
        _record_baseline(ctx, "turn start")

    def post_tool_call(tool_name: str = "", args=None, session_id: str = "", status: str = "",
                       **_kwargs) -> None:
        if tool_name in _KNOWLEDGE_TOOLS:
            reason = f"after {tool_name}"
        elif tool_name in _FILE_TOOLS and _touches_tracked_path(args):
            reason = f"after {tool_name}"
        else:
            return
        if _enabled(ctx):
            # post_tool_call carries no platform; use what this session's hooks reported.
            WORKER.submit(resolve_home(), resolve_data_dir(), read_options(ctx), reason, session_id,
                          platform=SESSIONS.lookup(session_id),
                          call=describe_call(tool_name, args, status))

    def on_session_end(session_id: str = "", platform: str = "", **_kwargs) -> None:
        # Catches writes made during the turn that bypass tools, such as terminal edits.
        SESSIONS.remember(session_id, platform)
        if _enabled(ctx):
            WORKER.submit(resolve_home(), resolve_data_dir(), read_options(ctx), "turn end", session_id,
                          platform=platform or "")

    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)

    from .chat import COMMAND, handle_slash
    from .cli import build_parser, run_cli

    # Read-only view for chat surfaces (Telegram, Discord, Desktop, TUI, CLI chat); restore stays on the host.
    ctx.register_command(COMMAND, handler=lambda raw_args: handle_slash(raw_args, ctx),
                         description="Show the version history of memory, skills and SOUL.md (read-only)",
                         args_hint="[memory|user|soul|skills/<path>|<version> [target]]")
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
