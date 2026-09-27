"""Shared fixtures. The plugin directory has a hyphen, so load it under an importable name."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "memory-rewind"


def _load_plugin_package():
    name = "memory_rewind"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mrw = _load_plugin_package()


@pytest.fixture
def hostile_git_env(tmp_path, monkeypatch):
    """A user git setup that would break a naive integration: signing on, a failing
    commit hook, identity required, and GIT_DIR pointing somewhere else."""
    user_home = tmp_path / "userhome"
    hooks = user_home / "hooks"
    hooks.mkdir(parents=True)
    marker = user_home / "HOOK_RAN"
    for hook in ("pre-commit", "commit-msg", "reference-transaction", "post-commit"):
        script = hooks / hook
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", newline="\n")
        script.chmod(0o755)
    (user_home / ".gitconfig").write_text(
        "[commit]\n\tgpgsign = true\n[core]\n\thooksPath = " + str(hooks) + "\n"
        "[user]\n\tuseConfigOnly = true\n", newline="\n")
    decoy = tmp_path / "decoy.git"
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("GIT_DIR", str(decoy))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "decoy-index"))
    return marker, decoy


@pytest.fixture
def home(tmp_path, hostile_git_env):
    root = tmp_path / "hermes-home"
    (root / "memories").mkdir(parents=True)
    (root / "memories" / "MEMORY.md").write_text("first note\n", newline="\n")
    (root / "memories" / "USER.md").write_text("likes tea\n", newline="\n")
    (root / "SOUL.md").write_text("You are Hermes.\n", newline="\n")
    skill = root / "skills" / "productivity" / "notes"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: notes\n---\nTake notes.\n", newline="\n")
    (skill / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n", newline="\n")
    (skill / "scripts" / "run.sh").chmod(0o755)
    return root


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "plugin-data" / "memory-rewind"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def options():
    return mrw.TrackingOptions()


@pytest.fixture
def store(home, data_dir):
    from memory_rewind.gitstore import GitStore
    return GitStore(home, data_dir)


def snapshot(home, data_dir, options, reason="test"):
    from memory_rewind.worker import take_snapshot
    return take_snapshot(home, data_dir, options, [reason])


@pytest.fixture
def snap(home, data_dir, options):
    return lambda reason="test": snapshot(home, data_dir, options, reason)


def pytest_report_header(config):
    return f"memory-rewind plugin dir: {PLUGIN_DIR}; git: {os.popen('git --version').read().strip()}"
