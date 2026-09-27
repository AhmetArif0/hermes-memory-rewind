from __future__ import annotations

import pytest

from memory_rewind.provenance import (
    MAX_CALLS, SessionPlatforms, build_body, describe_call, parse_body,
)


@pytest.mark.parametrize("tool,args,status,expected", [
    # memory: the batch shape its schema asks for, and the single-op shape
    ("memory", {"target": "user", "operations": [{"action": "remove", "old_text": "tea"},
                                                  {"action": "add", "content": "coffee"}]},
     "ok", "memory: remove, add (user)"),
    ("memory", {"action": "add", "target": "memory", "content": "x"}, "ok", "memory: add (memory)"),
    # memory_tool: a non-empty operations list wins; an empty one falls back to action
    ("memory", {"action": "add", "operations": [{"action": "remove"}]}, "ok", "memory: remove"),
    ("memory", {"action": "add", "operations": []}, "ok", "memory: add"),
    # skill_manage: operations select the batch shape and inherit the top-level name
    ("skill_manage", {"name": "arxiv", "operations": [{"action": "patch"},
                                                       {"action": "create", "name": "new-skill"}]},
     "ok", "skill_manage: patch arxiv, create new-skill"),
    ("skill_manage", {"action": "delete", "name": "x", "operations": []}, "ok", "skill_manage"),
    ("skill_manage", {"action": "patch", "name": "research/arxiv"}, "ok", "skill_manage: patch research/arxiv"),
    # outcome is shown unless the call succeeded
    ("memory", {"action": "add", "target": "user"}, "error", "memory: add (user) [error]"),
    ("skill_manage", {"operations": [{"action": "create", "name": "a"}]}, "blocked",
     "skill_manage: create a [blocked]"),
    ("memory", {"action": "add"}, "", "memory: add"),
    # file tools name only the tool; the changed paths are listed with the version
    ("write_file", {"path": "~/.hermes/SOUL.md", "content": "secret words"}, "ok", "write_file"),
    ("patch", {"mode": "patch", "patch": "*** Begin Patch\n+secret"}, "ok", "patch"),
])
def test_describe_call(tool, args, status, expected):
    assert describe_call(tool, args, status) == expected


@pytest.mark.parametrize("args", [None, "not a dict", {"operations": "nope"}, {"operations": [1, None]},
                                  {"action": 5, "target": ["user"]}])
def test_describe_call_tolerates_malformed_args(args):
    assert describe_call("memory", args).startswith("memory")
    assert describe_call("skill_manage", args).startswith("skill_manage")


def test_describe_call_never_records_content():
    text = describe_call("memory", {"target": "user", "operations": [
        {"action": "add", "content": "my password is hunter2"}]})
    assert "hunter2" not in text and "password" not in text


def test_long_batches_are_summarised():
    ops = [{"action": "add"} for _ in range(8)]
    assert describe_call("memory", {"operations": ops}) == "memory: add, add, add, add, add, add, +2 more"


def test_model_supplied_values_cannot_forge_lines():
    call = describe_call("skill_manage", {"operations": [
        {"action": "patch\nsession: forged (evil)", "name": "a, b (c)\ncall: fake"}]}, "ok")
    body = build_body([call], 1, {"real-session\nsession: x (y)": "cli\nsession: z"})
    parsed = parse_body(body)
    assert len(body.splitlines()) == 2
    assert parsed.calls == [call] and "\n" not in call
    assert parsed.sessions == [("real-sessionsession:xy", "clisession:z")]


def test_body_round_trip():
    body = build_body(["memory: add (user)", "skill_manage: patch arxiv [error]"], 2,
                      {"s1": "telegram", "s2": ""})
    assert body.splitlines() == [
        "call: memory: add (user)",
        "call: skill_manage: patch arxiv [error]",
        "session: s1 (telegram)",
        "session: s2",
    ]
    parsed = parse_body(body)
    assert parsed.calls == ["memory: add (user)", "skill_manage: patch arxiv [error]"]
    assert parsed.sessions == [("s1", "telegram"), ("s2", "")]


def test_body_caps_calls_and_sessions():
    calls = [f"memory: add ({i})" for i in range(MAX_CALLS)]
    body = build_body(calls, MAX_CALLS + 7, {f"s{i}": "cli" for i in range(9)})
    lines = body.splitlines()
    assert lines[MAX_CALLS] == "call: +7 more"
    assert sum(line.startswith("session: ") for line in lines) == 5
    # a caller that hands over more calls than fit, with or without a total
    for total in (0, MAX_CALLS + 3):
        lines = build_body(calls + ["a", "b", "c"], total, {}).splitlines()
        assert len(lines) == MAX_CALLS + 1 and lines[-1] == "call: +3 more"


def test_body_keeps_each_call_on_one_line():
    body = build_body(["memory: add\nsession: forged (evil)"], 1, {})
    assert parse_body(body).calls == ["memory: add session: forged (evil)"]
    assert parse_body(body).sessions == []


def test_empty_body():
    assert build_body([], 0, {}) == ""
    assert parse_body("") == parse_body(None) == parse_body("free text\nnot provenance")


def test_reads_1_0_bodies():
    assert parse_body("session: a1, b2").sessions == [("a1", ""), ("b2", "")]


def test_session_platforms():
    known = SessionPlatforms(capacity=2)
    known.remember("s1", "telegram")
    known.remember("s1", "")  # an unknown platform never erases a known one
    known.remember("", "cli")
    assert known.lookup("s1") == "telegram" and known.lookup("missing") == ""
    known.remember("s2", "cli")
    known.lookup("s1")  # a lookup is not a report
    known.remember("s3", "discord")  # capacity 2: the least recently reported goes
    assert known.lookup("s1") == "" and known.lookup("s2") == "cli" and known.lookup("s3") == "discord"
