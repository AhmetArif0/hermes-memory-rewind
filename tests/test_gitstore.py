from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from memory_rewind.gitstore import REJOIN_REASON, GitError, GitStore
from memory_rewind.tracking import collect_tracked_files
from memory_rewind.worker import take_snapshot


def test_first_snapshot_then_no_change(store, snap):
    first = snap("baseline")
    assert first and store.head() == first
    assert snap("again") is None
    assert store.commit_count() == 1


def test_records_modify_add_delete(home, store, snap):
    snap("baseline")
    (home / "memories" / "MEMORY.md").write_text("first note\nsecond note\n", newline="\n")
    new_skill = home / "skills" / "dev" / "lint" / "SKILL.md"
    new_skill.parent.mkdir(parents=True)
    new_skill.write_text("lint", newline="\n")
    (home / "skills" / "productivity" / "notes" / "scripts" / "run.sh").unlink()
    assert snap("change")
    changes = dict((path, status) for status, path in store.log(limit=1)[0].changes)
    assert changes == {
        "memories/MEMORY.md": "M",
        "skills/dev/lint/SKILL.md": "A",
        "skills/productivity/notes/scripts/run.sh": "D",
    }
    assert store.read_blob("HEAD", "memories/MEMORY.md") == b"first note\nsecond note\n"


def test_newly_excluded_file_leaves_history(home, store, data_dir):
    from memory_rewind.worker import take_snapshot
    from memory_rewind.tracking import TrackingOptions
    take_snapshot(home, data_dir, TrackingOptions(), ["baseline"])
    take_snapshot(home, data_dir, TrackingOptions(track_soul=False), ["soul off"])
    assert "SOUL.md" not in store.ls_tree("HEAD", "SOUL.md")


def test_user_git_setup_cannot_interfere(home, store, snap, hostile_git_env, tmp_path):
    hook_marker, decoy = hostile_git_env
    assert snap("baseline")
    (home / "SOUL.md").write_text("changed", newline="\n")
    assert snap("change")
    assert not hook_marker.exists(), "a user git hook ran"
    assert not decoy.exists(), "inherited GIT_DIR was used"
    assert not (tmp_path / "decoy-index").exists(), "inherited GIT_INDEX_FILE was used"


@pytest.mark.skipif(os.name == "nt", reason="*, ? and : are not valid in Windows file names")
def test_literal_pathspecs_for_glob_like_names(home, store, snap, options):
    odd = home / "skills" / "cat" / "odd" / "[a]*?:name.md"
    odd.parent.mkdir(parents=True)
    odd.write_text("literal", newline="\n")
    (home / "skills" / "cat" / "odd" / "a.md").write_text("must not be matched by the glob", newline="\n")
    (home / "skills" / "cat" / "odd" / "ü n i.md").write_text("unicode", newline="\n")
    snap("odd names")
    tree = store.ls_tree("HEAD", "skills/cat/odd")
    assert set(tree) == {"skills/cat/odd/[a]*?:name.md", "skills/cat/odd/a.md", "skills/cat/odd/ü n i.md"}
    odd.unlink()
    snap("odd removed")
    assert set(store.ls_tree("HEAD", "skills/cat/odd")) == {"skills/cat/odd/a.md", "skills/cat/odd/ü n i.md"}


@pytest.mark.skipif(os.name == "nt", reason="*, ? and : are not valid in Windows file names")
def test_glob_like_target_selects_only_itself(home, store, snap):
    """A skill directory literally named "x*" must not also select its sibling "xy" when
    listing, logging or diffing: as git pathspec magic, "x*" would match across "/"."""
    for name in ("x*", "xy"):
        skill = home / "skills" / "cat" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(name, newline="\n")
    first = snap("two skills")
    assert set(store.ls_tree("HEAD", "skills/cat/x*")) == {"skills/cat/x*/SKILL.md"}
    (home / "skills" / "cat" / "xy" / "SKILL.md").write_text("only the sibling changed", newline="\n")
    snap("sibling edit")
    assert store.log(["skills/cat/x*"], limit=10)[0].sha == first
    assert store.diff(first, "HEAD", ["skills/cat/x*"]) == ""


def test_lost_race_retries_on_top_of_the_rival(home, data_dir, options, monkeypatch):
    """Another process publishes a different tree between our staging and our update-ref:
    the compare-and-swap must fail, and we must retry on top of its commit instead of
    overwriting it."""
    store = GitStore(home, data_dir)
    store.snapshot(collect_tracked_files(home, options), "baseline")
    rival = GitStore(home, data_dir)
    rival_commit = {}

    real_stage = GitStore._stage

    def racing_stage(self, index, files):
        tree = real_stage(self, index, files)
        if self is store and not rival_commit:
            # The rival publishes a tree without USER.md (a genuinely different tree).
            without_user = [p for p in collect_tracked_files(home, options) if p != "memories/USER.md"]
            rival_commit["sha"] = rival.snapshot(without_user, "rival")
        return tree

    monkeypatch.setattr(GitStore, "_stage", racing_stage)
    (home / "memories" / "USER.md").write_text("likes coffee", newline="\n")
    ours = store.snapshot(collect_tracked_files(home, options), "ours")

    assert rival_commit["sha"] and ours and ours != rival_commit["sha"]
    assert [c.subject for c in store.log(limit=10)] == ["ours", "rival", "baseline"]
    parent = store._out(["rev-parse", f"{ours}^"], with_worktree=False)
    assert parent == rival_commit["sha"], "retry must build on the rival's commit"
    assert store.read_blob("HEAD", "memories/USER.md") == b"likes coffee"
    assert "memories/USER.md" not in store.ls_tree(rival_commit["sha"], "memories")


@pytest.mark.parametrize("bad", ["-n", "--all", "HEAD --", "", "nope", "HEAD~99"])
def test_resolve_rejects_bad_revisions(store, snap, bad):
    snap("baseline")
    with pytest.raises(GitError):
        store.resolve(bad)


def test_destroy_forgets_history(store, snap):
    snap("baseline")
    store.destroy()
    assert not store.repo.exists() and not store.tips.exists() and store.head() is None
    assert snap("fresh")  # history can start again


def test_stat_cache_is_optional(store, snap, home):
    snap("baseline")
    store.cache_index.unlink()
    (home / "SOUL.md").write_text("after cache loss", newline="\n")
    assert snap("change")
    assert store.read_blob("HEAD", "SOUL.md") == b"after cache loss"
    assert snap("again") is None


def test_same_size_edit_in_the_same_second_is_recorded(home, store, snap):
    """git trusts a cached stat entry unless the index is as new as the file ("racy git"). The cached
    index must keep its own write time when copied, or an edit of the same size in the same second as
    the previous snapshot is never recorded (the newest version then keeps the old content)."""
    import time
    soul = home / "SOUL.md"
    then = time.time() - 3600  # a fixed second both writes and the index share
    soul.write_text("version A\n", newline="\n")
    os.utime(soul, (then, then))
    snap("A")
    # Deterministic: compare mtime/size/inode only, and date the index to the same second.
    store._run(["config", "--local", "core.trustctime", "false"], with_worktree=False)
    os.utime(store.cache_index, (then, then))
    soul.write_text("version B\n", newline="\n")
    os.utime(soul, (then, then))
    assert snap("B"), "the changed file was not recorded"
    assert store.read_blob("HEAD", "SOUL.md") == b"version B\n"


def test_cached_index_keeps_gits_write_time(store, snap, monkeypatch):
    """Promoting the stat cache must keep the time git wrote the index (see the test above)."""
    import time
    then = int(time.time()) - 3600
    real_stage = GitStore._stage

    def stage_dated(self, index, files):
        tree = real_stage(self, index, files)
        os.utime(index, (then, then))  # as if git had written this index an hour ago
        return tree

    monkeypatch.setattr(GitStore, "_stage", stage_dated)
    snap("baseline")
    assert int(store.cache_index.stat().st_mtime) == then


def test_repo_never_detaches_background_gc(store, snap):
    snap("baseline")
    assert store._out(["config", "--local", "--get", "gc.autoDetach"], with_worktree=False) == "false"


def test_periodic_gc_command_succeeds(store, snap, monkeypatch):
    """_maybe_gc swallows failures on purpose; prove the command itself is valid and runs."""
    import memory_rewind.gitstore as gs
    calls = []
    real_run = gs.GitStore._run

    def recording_run(self, args, **kwargs):
        proc = real_run(self, args, **kwargs)
        if "gc" in args:
            calls.append((args, proc.returncode, proc.stderr))
        return proc

    monkeypatch.setattr(gs, "_GC_EVERY_N_COMMITS", 1)
    monkeypatch.setattr(gs.GitStore, "_run", recording_run)
    snap("baseline")
    assert calls, "gc was not attempted"
    args, code, err = calls[-1]
    assert args[:3] == ["-c", "gc.autoDetach=false", "gc"] and code == 0, err


# ── an older copy of the history put back over it (hermes import of a backup) ──────────────

def _older_copy(data_dir, tmp_path):
    """What `hermes backup` saves of the plugin's data: the whole directory."""
    copy = tmp_path / "backup-copy"
    shutil.copytree(data_dir, copy)
    return copy


def _put_back(copy, data_dir):
    """What `hermes import` does with it: replace every file the copy holds (temp file +
    os.replace, as Hermes extracts) and leave every other file alone. A file already holding
    the same bytes is left as is: git objects are named by their content and read-only, and
    Windows refuses to replace a read-only file."""
    for src in sorted(copy.rglob("*")):
        if src.is_dir():
            continue
        dest = data_dir / src.relative_to(copy)
        if dest.is_file() and dest.read_bytes() == src.read_bytes():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent)
        os.close(fd)
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)


def _parents(store, rev):
    return store._out(["rev-list", "--parents", "--max-count=1", rev], with_worktree=False).split()[1:]


@pytest.mark.parametrize("backup_has_markers", [True, False], ids=["backup-by-1.3.1", "backup-by-1.3.0"])
def test_an_older_copy_put_back_keeps_later_versions(home, store, snap, data_dir, tmp_path, monkeypatch,
                                                     backup_has_markers):
    # Same-second versions: with two starts, a plain date walk listed the parent first.
    monkeypatch.setenv("GIT_COMMITTER_DATE", "1790000000 +0000")
    first = snap("first")
    copy = _older_copy(data_dir, tmp_path)
    if not backup_has_markers:
        shutil.rmtree(copy / "tips")
    memory = home / "memories" / "MEMORY.md"
    memory.write_text("first note\n§\nsecond note", newline="\n")
    second = snap("second")
    memory.write_text("first note\n", newline="\n")  # the import writes the old file back too
    _put_back(copy, data_dir)

    assert store.head() == first, "the copy points the history back at the backup"
    assert [c.sha for c in store.log()] == [second, first], "reads still include the cut-off version"
    assert store.commit_count() == 2

    rejoined = snap("session start")
    assert _parents(store, rejoined) == [second], "the next version builds on the cut-off tip"
    newest = store.log(limit=1)[0]
    assert newest.subject == f"session start; {REJOIN_REASON}"
    assert newest.changes == [("M", "memories/MEMORY.md")], "the import is recorded as a change"
    assert store.read_blob(f"{rejoined}~1", "memories/MEMORY.md") == b"first note\n\xc2\xa7\nsecond note"
    assert os.listdir(store.tips) == [rejoined]
    assert snap("turn start") is None


def test_an_older_copy_with_unchanged_files_moves_the_head_back(home, store, snap, data_dir, tmp_path):
    snap("first")
    copy = _older_copy(data_dir, tmp_path)
    (home / "SOUL.md").write_text("You are Hermes, v2.\n", newline="\n")
    second = snap("second")
    _put_back(copy, data_dir)  # files on disk are left as they were
    assert snap("turn start") is None
    assert store.head() == second and os.listdir(store.tips) == [second]


def test_an_unrelated_copy_is_joined_not_dropped(home, store, snap, data_dir, options, tmp_path):
    ours = snap("ours")
    other_home, other_data = tmp_path / "other-home", tmp_path / "other-data"
    shutil.copytree(home, other_home)
    (other_home / "SOUL.md").write_text("Another machine's soul.\n", newline="\n")
    theirs = take_snapshot(other_home, other_data, options, ["theirs"])
    _put_back(other_data, data_dir)  # a backup from a different history (another machine)
    assert store.head() == theirs

    (home / "memories" / "USER.md").write_text("likes coffee\n", newline="\n")
    joined = snap("turn start")
    assert _parents(store, joined) == [ours, theirs], "both lines stay reachable, ours first"
    newest = store.log(limit=1)[0]
    assert newest.changes == [("M", "memories/USER.md")], "a join lists its changes against ours"
    assert "likes coffee" in store.changes(joined) and "Another machine" not in store.changes(joined)
    assert {theirs, ours} <= {c.sha for c in store.log(limit=10)}


def test_a_history_without_markers_gets_one_without_a_new_version(store, snap):
    head = snap("first")
    shutil.rmtree(store.tips)  # a history written before markers existed
    assert snap("turn start") is None
    assert os.listdir(store.tips) == [head]


def test_markers_cost_no_git_call_in_the_usual_case(store, snap, monkeypatch):
    head = snap("first")
    calls = []
    real_run = GitStore._run

    def counting_run(self, args, **kwargs):
        calls.append(args[0])
        return real_run(self, args, **kwargs)

    monkeypatch.setattr(GitStore, "_run", counting_run)
    assert store._classify_markers(head) == ([], [])
    assert calls == []


def test_stale_and_foreign_markers_are_dropped(store, snap, home):
    first = snap("first")
    (home / "SOUL.md").write_text("changed\n", newline="\n")
    second = snap("second")
    (store.tips / first).touch()  # left behind by a process that lost the race to mark
    (store.tips / ("f" * 40)).touch()  # names no commit in this history
    (store.tips / "README").touch()  # not a marker at all
    lost, stale = store._classify_markers(second)
    assert lost == [] and sorted(stale) == sorted([first, "f" * 40])
    (home / "SOUL.md").write_text("changed again\n", newline="\n")
    third = snap("third")
    assert sorted(os.listdir(store.tips)) == sorted([third, "README"])
