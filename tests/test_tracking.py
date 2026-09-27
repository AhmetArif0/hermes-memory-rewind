from __future__ import annotations

import os

import pytest

from memory_rewind.tracking import (
    TrackingOptions, collect_tracked_files, is_trackable_path, is_trackable_target,
)


def _touch(path, text="x", size=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size if size is not None else text.encode())


def test_tracks_memory_soul_and_skills(home, options):
    assert collect_tracked_files(home, options) == [
        "SOUL.md",
        "memories/MEMORY.md",
        "memories/USER.md",
        "skills/productivity/notes/SKILL.md",
        "skills/productivity/notes/scripts/run.sh",
    ]


def test_tracks_curator_archive(home, options):
    _touch(home / "skills" / ".archive" / "old-skill" / "SKILL.md", "archived")
    assert "skills/.archive/old-skill/SKILL.md" in collect_tracked_files(home, options)


@pytest.mark.parametrize("rel", [
    "skills/.usage.json",
    "skills/.curator_ledger.jsonl",
    "skills/.bundled_manifest",
    "skills/.curator_suppressed",
    "skills/.curator_suppressed_2026",
    "skills/.curator_backups/run-1.tar.gz",
    "skills/.hub/lock.json",
    "skills/.hub/quarantine/evil/SKILL.md",
    "skills/.hub/index-cache/x.json",
    "skills/cat/s/.env",
    "skills/cat/s/.env.local",
    "skills/cat/s/id_rsa",
    "skills/cat/s/deploy.pem",
    "skills/cat/s/api.key",
    "skills/cat/s/auth.json",
    "skills/cat/s/credentials.json",
    "skills/cat/s/cache.db",
    "skills/cat/s/cache.sqlite3",
    "skills/cat/s/__pycache__/m.cpython-311.pyc",
    "skills/cat/s/node_modules/pkg/index.js",
    "skills/cat/s/.DS_Store",
])
def test_never_tracks_bookkeeping_secrets_and_noise(home, options, rel):
    _touch(home / rel)
    assert rel not in collect_tracked_files(home, options)
    assert not is_trackable_path(rel, options)


def test_only_known_top_level_files(home, options):
    for rel in ("config.yaml", ".env", "auth.json", "state.db", "memories/other.md", "logs/agent.log"):
        _touch(home / rel)
    tracked = collect_tracked_files(home, options)
    for rel in ("config.yaml", ".env", "auth.json", "state.db", "memories/other.md", "logs/agent.log"):
        assert rel not in tracked


def test_size_cap(home):
    _touch(home / "skills" / "cat" / "big" / "blob.bin", size=2048)
    _touch(home / "skills" / "cat" / "big" / "small.bin", size=1024)
    tracked = collect_tracked_files(home, TrackingOptions(max_file_kb=1))
    assert "skills/cat/big/small.bin" in tracked
    assert "skills/cat/big/blob.bin" not in tracked


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_skips_symlinks_and_embedded_repos(home, options, tmp_path):
    outside = tmp_path / "outside"
    _touch(outside / "SKILL.md", "outside")
    (home / "skills" / "linked-dir").symlink_to(outside, target_is_directory=True)
    (home / "skills" / "productivity" / "notes" / "link.md").symlink_to(outside / "SKILL.md")
    _touch(home / "skills" / "vendored" / "repo" / "SKILL.md")
    (home / "skills" / "vendored" / "repo" / ".git").mkdir()
    tracked = collect_tracked_files(home, options)
    assert not any("linked-dir" in p or p.endswith("link.md") or "vendored/repo" in p for p in tracked)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_skips_memory_file_behind_symlinked_dir(home, options, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "MEMORY.md").write_text("elsewhere", newline="\n")
    (home / "memories" / "MEMORY.md").unlink()
    (home / "memories" / "USER.md").unlink()
    (home / "memories").rmdir()
    (home / "memories").symlink_to(real, target_is_directory=True)
    assert "memories/MEMORY.md" not in collect_tracked_files(home, options)


def test_options_turn_off_soul_and_skills(home):
    tracked = collect_tracked_files(home, TrackingOptions(track_skills=False, track_soul=False))
    assert tracked == ["memories/MEMORY.md", "memories/USER.md"]


@pytest.mark.parametrize("rel,ok", [
    ("skills", True), ("skills/productivity/notes", True), ("memories/MEMORY.md", True),
    ("SOUL.md", True), ("../SOUL.md", False), ("skills/../config.yaml", False), ("/etc/passwd", False),
    ("skills/.hub", False), ("skills/.hub/quarantine", False), ("skills/.curator_backups", False),
    ("config.yaml", False), ("memories", False), ("skills/cat/.env", False),
])
def test_restore_targets(options, rel, ok):
    assert is_trackable_target(rel, options) is ok
