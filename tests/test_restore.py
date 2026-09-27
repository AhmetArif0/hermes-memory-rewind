from __future__ import annotations

import os

import pytest

from memory_rewind.restore import (
    RestoreError, apply_restore, normalize_target, plan_restore,
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
