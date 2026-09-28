from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

import pytest

import memory_rewind.restore as restore_mod
from memory_rewind.restore import (
    RestoreError, apply_restore, memory_entry_changes, normalize_target, plan_restore,
)


def _restore(store, home, options, rev, target):
    plan = plan_restore(store, home, rev, normalize_target(target, home, options), options)
    apply_restore(store, home, plan)
    return plan


def test_restore_memory_file(home, store, snap, options):
    v1 = snap("v1")
    (home / "memories" / "MEMORY.md").write_text("", newline="\n")  # the #105684 shape: an emptied file
    snap("emptied")
    plan = _restore(store, home, options, v1, "memory")
    assert list(plan.write) == ["memories/MEMORY.md"] and not plan.delete
    assert (home / "memories" / "MEMORY.md").read_text() == "first note\n"
    assert (home / "memories" / "USER.md").read_text() == "likes tea\n"  # untouched


def test_restore_skill_dir_writes_and_removes(home, store, snap, options):
    v1 = snap("v1")
    skill = home / "skills" / "productivity" / "notes"
    (skill / "SKILL.md").write_text("broken edit", newline="\n")
    (skill / "references").mkdir()
    (skill / "references" / "extra.md").write_text("added later", newline="\n")
    (skill / "scripts" / "run.sh").unlink()
    snap("broken")
    plan = _restore(store, home, options, v1, "skills/productivity/notes")
    assert set(plan.write) == {"skills/productivity/notes/SKILL.md",
                               "skills/productivity/notes/scripts/run.sh"}
    assert plan.delete == ["skills/productivity/notes/references/extra.md"]
    assert (skill / "SKILL.md").read_text() == "---\nname: notes\n---\nTake notes.\n"
    assert not (skill / "references").exists(), "emptied directory should be pruned"
    if os.name != "nt":
        assert os.stat(skill / "scripts" / "run.sh").st_mode & 0o111, "executable bit restored"


def test_restore_deleted_skill(home, store, snap, options):
    import shutil
    v1 = snap("v1")
    shutil.rmtree(home / "skills" / "productivity" / "notes")
    snap("deleted")
    _restore(store, home, options, v1, "skills/productivity/notes")
    assert (home / "skills" / "productivity" / "notes" / "SKILL.md").exists()


def test_restore_archived_skill_back(home, store, snap, options):
    """The #83580 shape: the curator moved a skill into skills/.archive and it will not come back."""
    import shutil
    v1 = snap("v1")
    archive = home / "skills" / ".archive" / "notes"
    archive.parent.mkdir(parents=True)
    shutil.move(str(home / "skills" / "productivity" / "notes"), str(archive))
    snap("curator archived notes")
    _restore(store, home, options, v1, "skills/productivity/notes")
    assert (home / "skills" / "productivity" / "notes" / "SKILL.md").read_text().endswith("Take notes.\n")


def test_restore_never_writes_back_files_excluded_today(home, store, snap, options):
    """1.0.0 recorded Hermes' skill lock files; restoring such a version must not recreate them."""
    from memory_rewind.tracking import collect_tracked_files
    lock = home / "skills" / ".locks" / "ledger.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("", newline="\n")
    old = store.snapshot(sorted(collect_tracked_files(home, options) + ["skills/.locks/ledger.lock"]),
                         "a 1.0.0 version")
    assert "skills/.locks/ledger.lock" in store.ls_tree(old, "skills")
    lock.unlink()
    (home / "skills" / "productivity" / "notes" / "SKILL.md").write_text("edited", newline="\n")
    snap("edit")
    plan = _restore(store, home, options, old, "skills")
    assert list(plan.write) == ["skills/productivity/notes/SKILL.md"]
    assert not lock.exists()


def test_already_matching_is_empty_plan(home, store, snap, options):
    v1 = snap("v1")
    plan = plan_restore(store, home, v1, "memories/MEMORY.md", options)
    assert plan.empty


def test_unchanged_siblings_are_not_rewritten(home, store, snap, options):
    v1 = snap("v1")
    (home / "skills" / "productivity" / "notes" / "SKILL.md").write_text("edited", newline="\n")
    snap("edit")
    plan = plan_restore(store, home, v1, "skills/productivity/notes", options)
    assert list(plan.write) == ["skills/productivity/notes/SKILL.md"]


@pytest.mark.parametrize("target", [
    "../SOUL.md", "skills/../config.yaml", "/etc/passwd", "config.yaml", ".env",
    "skills/.hub/quarantine/evil", "skills/.curator_backups", "memories", "",
])
def test_rejects_untracked_targets(home, options, target):
    with pytest.raises(RestoreError):
        normalize_target(target, home, options)


def test_absolute_path_inside_home_is_accepted(home, options):
    assert normalize_target(str(home / "memories" / "USER.md"), home, options) == "memories/USER.md"


def test_file_missing_at_revision_is_refused(home, store, snap, options):
    (home / "SOUL.md").unlink()
    v1 = snap("no soul yet")
    (home / "SOUL.md").write_text("now a soul", newline="\n")
    snap("soul added")
    with pytest.raises(RestoreError):
        plan_restore(store, home, v1, "SOUL.md", options)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_refuses_to_write_through_symlink(home, store, snap, options, tmp_path):
    v1 = snap("v1")
    (home / "skills" / "productivity" / "notes" / "SKILL.md").write_text("edited", newline="\n")
    snap("edit")
    plan = plan_restore(store, home, v1, "skills/productivity/notes", options)
    outside = tmp_path / "outside"
    outside.mkdir()
    real = home / "skills" / "productivity"
    real.rename(tmp_path / "moved")
    real.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RestoreError):
        apply_restore(store, home, plan)
    assert not any(outside.iterdir()), "nothing may be written outside HERMES_HOME"


# ── a restore applies exactly the plan it showed ─────────────────────────────────────────

@contextmanager
def _hermes_memory_lock(path):
    """Hold the lock the way Hermes' memory tool does (MemoryStore._file_lock), independently of the
    plugin's own implementation: <file>.lock, flock on POSIX, a one-byte msvcrt lock on Windows."""
    raw = os.open(path.with_suffix(path.suffix + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(raw, "r+", encoding="utf-8") as fd:
        if restore_mod.fcntl is not None:
            restore_mod.fcntl.flock(fd, restore_mod.fcntl.LOCK_EX)
        else:
            fd.seek(0)
            restore_mod.msvcrt.locking(fd.fileno(), restore_mod.msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if restore_mod.fcntl is not None:
                restore_mod.fcntl.flock(fd, restore_mod.fcntl.LOCK_UN)
            else:
                fd.seek(0)
                restore_mod.msvcrt.locking(fd.fileno(), restore_mod.msvcrt.LK_UNLCK, 1)


def _hold_in_thread(path, seconds):
    held = threading.Event()

    def run():
        with _hermes_memory_lock(path):
            held.set()
            time.sleep(seconds)

    thread = threading.Thread(target=run)
    thread.start()
    assert held.wait(5)
    return thread


def test_memory_file_changed_after_the_plan_is_not_replaced(home, store, snap, options):
    v1 = snap("v1")
    memory = home / "memories" / "MEMORY.md"
    memory.write_text("", newline="\n")
    snap("emptied")
    plan = plan_restore(store, home, v1, "memories/MEMORY.md", options)
    memory.write_text("the agent wrote this while the plan was on screen", newline="\n")
    with pytest.raises(RestoreError, match="changed after the restore plan was made"):
        apply_restore(store, home, plan)
    assert memory.read_text() == "the agent wrote this while the plan was on screen"


def test_skill_file_changed_after_the_plan_is_not_replaced(home, store, snap, options):
    v1 = snap("v1")
    skill_md = home / "skills" / "productivity" / "notes" / "SKILL.md"
    skill_md.write_text("---\nname: notes\n---\nEdited.\n", newline="\n")
    snap("edit")
    plan = plan_restore(store, home, v1, "skills/productivity/notes", options)
    skill_md.write_text("---\nname: notes\n---\nEdited again.\n", newline="\n")
    with pytest.raises(RestoreError):
        apply_restore(store, home, plan)
    assert skill_md.read_text().endswith("Edited again.\n")


def test_restore_waits_for_the_memory_tools_lock(home, store, snap, options):
    v1 = snap("v1")
    memory = home / "memories" / "MEMORY.md"
    memory.write_text("", newline="\n")
    snap("emptied")
    plan = plan_restore(store, home, v1, "memories/MEMORY.md", options)
    holder = _hold_in_thread(memory, 0.6)
    started = time.monotonic()
    apply_restore(store, home, plan)
    waited = time.monotonic() - started
    holder.join()
    assert waited >= 0.4, "the restore wrote while the memory tool held the lock"
    assert memory.read_text() == "first note\n"


def test_restore_gives_up_when_the_lock_stays_held(home, store, snap, options, monkeypatch):
    monkeypatch.setattr(restore_mod, "LOCK_WAIT_SECONDS", 0.3)
    v1 = snap("v1")
    user = home / "memories" / "USER.md"
    user.write_text("likes coffee\n", newline="\n")
    snap("changed")
    plan = plan_restore(store, home, v1, "memories/USER.md", options)
    holder = _hold_in_thread(user, 1.5)
    try:
        with pytest.raises(RestoreError, match="being written by a running Hermes"):
            apply_restore(store, home, plan)
    finally:
        holder.join()
    assert user.read_text() == "likes coffee\n"


def test_only_memory_files_take_the_memory_lock(home, store, snap, options):
    v1 = snap("v1")
    (home / "SOUL.md").write_text("changed\n", newline="\n")
    snap("changed")
    _restore(store, home, options, v1, "soul")
    assert not list(home.rglob("*.lock"))


@pytest.mark.parametrize("current, restored, back, gone", [
    (b"a\n\xc2\xa7\nb\n\xc2\xa7\nc", b"a\n\xc2\xa7\nd", ["d"], ["b", "c"]),
    (b"", b"first note\n", ["first note"], []),
    (b"a\n\xc2\xa7\nb", None, [], ["a", "b"]),
    (b"  a  \n\xc2\xa7\n\n\xc2\xa7\nb", b"b\n\xc2\xa7\na", [], []),
])
def test_memory_entry_changes(current, restored, back, gone):
    assert memory_entry_changes(current, restored) == (back, gone)


def test_a_filesystem_without_locks_fails_fast(home, store, snap, options, monkeypatch):
    import errno
    import types

    def no_locks(_fd, _op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(restore_mod, "fcntl", types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=no_locks))
    monkeypatch.setattr(restore_mod, "msvcrt", None)
    v1 = snap("v1")
    memory = home / "memories" / "MEMORY.md"
    memory.write_text("", newline="\n")
    snap("emptied")
    plan = plan_restore(store, home, v1, "memories/MEMORY.md", options)
    started = time.monotonic()
    with pytest.raises(RestoreError, match="cannot lock MEMORY.md.lock"):
        apply_restore(store, home, plan)
    assert time.monotonic() - started < 1, "a lock error is not a busy lock: no waiting"
    assert memory.read_text() == ""
