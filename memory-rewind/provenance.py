"""Where a version came from: the tool calls and sessions behind a snapshot.

Provenance is written into the commit message body, one fact per line:

    call: memory: remove, add (user)
    call: skill_manage: patch research/arxiv [error]
    session: 20260927_101200_ab12cd (telegram)

Every value comes from a hook payload, and action names, targets and skill names are
chosen by the model, so each one is reduced to a short token first. A token has no
spaces, newlines, commas or brackets, so it can never forge a line or a field. Message
content is never recorded; the file history already holds it.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_CALLS = 10  # call lines written per version
MAX_SESSIONS = 5  # session lines written per version
_MAX_ITEMS_PER_CALL = 6  # operations named per call line
_TOKEN_UNSAFE = re.compile(r"[^A-Za-z0-9_./:@+-]")
_SESSION_ITEM = re.compile(r"^([^\s(),]+)(?: \(([^\s(),]+)\))?$")


def token(value, limit: int = 64) -> str:
    """*value* reduced to a single safe word, or "" when nothing usable is left."""
    if not isinstance(value, str):
        return ""
    return _TOKEN_UNSAFE.sub("", value)[:limit]


def _named(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_ITEMS_PER_CALL])
    extra = len(items) - _MAX_ITEMS_PER_CALL
    return f"{shown}, +{extra} more" if extra > 0 else shown


def _memory_items(args: dict) -> list[str]:
    # memory_tool: a non-empty `operations` list wins over the single-op `action`.
    ops = args.get("operations")
    if isinstance(ops, list) and ops:
        return [token(op.get("action"), 32) or "?" for op in ops if isinstance(op, dict)]
    action = token(args.get("action"), 32)
    return [action] if action else []


def _skill_items(args: dict) -> list[str]:
    # skill_manage: any `operations` list selects the batch shape, and an operation
    # without a name falls back to the top-level `name`.
    ops = args.get("operations")
    if ops is not None:
        ops = [op for op in ops if isinstance(op, dict)] if isinstance(ops, list) else []
    else:
        ops = [args] if args.get("action") else []
    items = []
    for op in ops:
        action = token(op.get("action"), 32) or "?"
        name = token(op.get("name") or args.get("name"), 96)
        items.append(f"{action} {name}" if name else action)
    return items


def describe_call(tool_name: str, args, status: str = "") -> str:
    """One-line summary of a post_tool_call payload, e.g. "memory: add (user)"."""
    tool = token(tool_name, 32) or "tool"
    args = args if isinstance(args, dict) else {}
    items: list[str] = []
    if tool_name == "memory":
        items = _memory_items(args)
    elif tool_name == "skill_manage":
        items = _skill_items(args)
    text = f"{tool}: {_named(items)}" if items else tool
    if tool_name == "memory" and (target := token(args.get("target"), 32)):
        text += f" ({target})"
    outcome = token(status, 16)
    if outcome and outcome != "ok":
        text += f" [{outcome}]"
    return text


def build_body(calls: list[str], total_calls: int, sessions: dict[str, str]) -> str:
    """Commit body for a version recorded after *calls* (the first few of *total_calls*)
    in *sessions* (session id -> platform, "" when unknown)."""
    lines = [f"call: {' '.join(call.split())}" for call in calls[:MAX_CALLS]]
    hidden = max(total_calls, len(calls)) - len(lines)
    if hidden > 0:
        lines.append(f"call: +{hidden} more")
    for session_id, platform in list(sessions.items())[:MAX_SESSIONS]:
        sid, where = token(session_id, 96), token(platform, 32)
        if sid:
            lines.append(f"session: {sid} ({where})" if where else f"session: {sid}")
    return "\n".join(lines)


@dataclass
class Provenance:
    calls: list[str] = field(default_factory=list)
    sessions: list[tuple[str, str]] = field(default_factory=list)  # (session id, platform)


def parse_body(body: str) -> Provenance:
    """Read provenance back from a commit body. Lines it does not know are ignored."""
    found = Provenance()
    for line in (body or "").splitlines():
        if line.startswith("call: "):
            found.calls.append(line[len("call: "):].strip())
        elif line.startswith("session: "):
            # 1.0.0 wrote every id on one line: "session: a, b".
            for item in line[len("session: "):].split(","):
                match = _SESSION_ITEM.match(item.strip())
                if match:
                    found.sessions.append((match.group(1), match.group(2) or ""))
    return found


class SessionPlatforms:
    """Which platform recent sessions run on.

    post_tool_call carries no platform, so it is learned from on_session_start (first turn
    of a new session) and on_session_end (every turn). A session resumed in a fresh process
    is unknown until its first turn ends; unknown stays unknown, it is never guessed.
    """

    def __init__(self, capacity: int = 256) -> None:
        self._capacity = capacity
        self._known: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def remember(self, session_id, platform) -> None:
        sid, where = token(session_id, 96), token(platform, 32)
        if not sid or not where:
            return
        with self._lock:
            self._known[sid] = where
            self._known.move_to_end(sid)
            while len(self._known) > self._capacity:
                self._known.popitem(last=False)

    def lookup(self, session_id) -> str:
        with self._lock:
            return self._known.get(token(session_id, 96), "")
