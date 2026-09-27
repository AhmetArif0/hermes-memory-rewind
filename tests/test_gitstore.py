from __future__ import annotations

import os

import pytest

from memory_rewind.gitstore import GitError, GitStore
from memory_rewind.tracking import collect_tracked_files


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
    assert not store.repo.exists() and store.head() is None
    assert snap("fresh")  # history can start again


def test_stat_cache_is_optional(store, snap, home):
    snap("baseline")
    store.cache_index.unlink()
    (home / "SOUL.md").write_text("after cache loss", newline="\n")
    assert snap("change")
    assert store.read_blob("HEAD", "SOUL.md") == b"after cache loss"
    assert snap("again") is None
