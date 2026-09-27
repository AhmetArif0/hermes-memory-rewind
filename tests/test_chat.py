from __future__ import annotations

import pytest

from memory_rewind.chat import CHAT_LIMIT, USAGE, _is_target, render_history


@pytest.fixture
def view(home, data_dir, options):
    return lambda raw="": render_history(raw, home, data_dir, options)


def test_no_history_yet(view):
    assert view().startswith("No history yet.")


def test_recent_versions_with_provenance(home, data_dir, options, store, view):
    from memory_rewind.worker import take_snapshot
    base = take_snapshot(home, data_dir, options, ["session start"], {"s1": "telegram"})
    (home / "memories" / "USER.md").write_text("likes coffee\n", newline="\n")
    change = take_snapshot(home, data_dir, options, ["after memory"], {"s1": "telegram"},
                           ["memory: replace (user)"], 1)
    out = view()
    assert out.index(change[:10]) < out.index(base[:10])
    assert "    via   memory: replace (user)\n    from  telegram, session s1\n    M  memories/USER.md" in out
    assert "    … 2 more" in out  # the 5-file baseline lists 3 files in chat


def test_recent_versions_are_capped(home, snap, view):
    for i in range(12):
        (home / "SOUL.md").write_text(f"version {i}\n", newline="\n")
        snap(f"edit {i}")
    out = view()
    assert "edit 11" in out and "edit 2  " in out and "edit 1  " not in out


def test_versions_of_one_target(home, snap, view):
    snap("baseline")
    (home / "SOUL.md").write_text("You are terse.\n", newline="\n")
    snap("soul edit")
    assert "soul edit" in view("soul") and "soul edit" not in view("memory")


def test_what_a_version_changed(home, snap, view):
    snap("baseline")
    (home / "memories" / "MEMORY.md").write_text("first note\nsecond note\n", newline="\n")
    rev = snap("after memory")
    out = view(rev[:10])
    assert f"restore {rev[:10]}~1 memories/MEMORY.md" in out  # the one file it changed
    assert "+second note" in out and "SOUL.md" not in out and "truncated" not in out
    assert "    M  " not in out and "    … " not in out  # the patch lists the files; no file list above it
    (home / "SOUL.md").write_text("two files now\n", newline="\n")
    (home / "memories" / "USER.md").write_text("", newline="\n")
    both = snap("two changes")
    assert f"restore {both[:10]}~1 <target>" in view(both)
    assert "This version did not change memories/USER.md." in view(f"{rev[:10]} user")
    assert f"restore {rev[:10]}~1 memories/MEMORY.md" in view(f"{rev[:10]} memory")


def test_first_version_has_no_before(snap, view):
    rev = snap("baseline")
    out = view(rev)
    assert "~1" not in out and "+first note" in out


def test_long_output_fits_a_chat_message(home, snap, view):
    snap("baseline")
    big = home / "skills" / "research" / "big" / "SKILL.md"
    big.parent.mkdir(parents=True)
    big.write_text("".join(f"line {i} of a long skill\n" for i in range(600)), newline="\n")
    rev = snap("big skill")
    r = rev[:10]
    for raw, full in ((rev, f"hermes memory-rewind diff {r}~1 --to {r}"),
                      (f"{rev} skills/research/big", f"hermes memory-rewind diff {r}~1 skills/research/big --to {r}")):
        out = view(raw)
        assert len(out) <= CHAT_LIMIT
        assert out.endswith(f"\n… (truncated; full output on the host: {full})")
        assert "+line 0 of a long skill" in out  # cut at the end, not the start


def test_viewing_never_records_a_version(home, store, snap, view):
    rev = snap("baseline")
    (home / "SOUL.md").write_text("changed on disk, not yet recorded\n", newline="\n")
    for raw in ("", "memory", "soul", rev, f"{rev} soul", "nope", "help"):
        view(raw)
    assert store.commit_count() == 1


@pytest.mark.parametrize("raw", ["nope", "--all", "-n", "HEAD~99"])
def test_bad_versions_are_reported_with_usage(snap, view, raw):
    snap("baseline")
    out = view(raw)
    assert out.startswith("memory-rewind: ") and "revision" in out and out.endswith(USAGE)


def test_replies_never_reveal_host_paths(home, snap, view, monkeypatch):
    """A reply can land in a group chat: no host path the user did not type, no raw git error."""
    rev = snap("baseline")
    # The CLI would say "... is outside the Hermes home (<home>)" for these.
    for raw in (f"{rev} /etc/passwd", "/etc/passwd", f"{rev} ../config.yaml"):
        out = view(raw)
        assert str(home) not in out and out.endswith(USAGE)
    import memory_rewind.gitstore as gs

    def broken_log(*_args, **_kwargs):
        raise gs.GitError(f"git log failed: fatal: {home}/history.git is corrupt")

    monkeypatch.setattr(gs.GitStore, "log", broken_log)
    out = view()
    assert "corrupt" not in out and str(home) not in out and "Hermes log" in out


def test_git_missing(view, monkeypatch):
    import memory_rewind.gitstore as gs
    monkeypatch.setattr(gs, "find_git", lambda: None)
    assert "needs git" in view()


@pytest.mark.parametrize("raw", ["help", "--help", "a b c"])
def test_usage(view, raw):
    assert view(raw) == USAGE


@pytest.mark.parametrize("arg,is_target", [
    ("memory", True), ("USER", True), ("soul", True), ("SOUL.md", True), ("memories/MEMORY.md", True),
    ("skills", True), ("skills/research/arxiv", True), ("skills\\research\\arxiv", True),
    ("3f2a9c1b7e", False), ("HEAD~1", False), ("/etc/passwd", False), ("config.yaml", False),
])
def test_target_or_version(arg, is_target):
    assert _is_target(arg) is is_target
