"""End-to-end checks against a real Hermes checkout (skipped when Hermes is not importable).

Mirrors Hermes' own external-plugin contract tests: the plugin is copied into an isolated
HERMES_HOME, enabled in config.yaml, loaded by the real PluginManager, and driven through
real hook dispatch and the real memory tool.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

hermes_plugins = pytest.importorskip("hermes_cli.plugins")
try:  # newer Hermes ships hermes_yaml instead of depending on PyYAML
    import hermes_yaml as yaml
except ImportError:
    yaml = pytest.importorskip("yaml")

from conftest import PLUGIN_DIR  # noqa: E402

PLUGIN_KEY = "memory-rewind"


def _install(home: Path, settings: dict | None = None) -> None:
    shutil.copytree(PLUGIN_DIR, home / "plugins" / PLUGIN_KEY,
                    ignore=shutil.ignore_patterns("__pycache__"))
    cfg = {"plugins": {"enabled": [PLUGIN_KEY]}}
    if settings is not None:
        cfg["plugins"]["entries"] = {PLUGIN_KEY: {"settings": settings}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8", newline="\n")


@pytest.fixture
def hermes_env(home, tmp_path, monkeypatch):
    bundled = tmp_path / "bundled-plugins"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    return home


def _load(home: Path, settings: dict | None = None):
    _install(home, settings)
    manager = hermes_plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.error is None, loaded.error
    return manager, loaded


def _history(home: Path):
    sys.modules.pop("memory_rewind_probe", None)
    from memory_rewind.gitstore import GitStore
    return GitStore(home, home / "plugin-data" / PLUGIN_KEY)


def test_loads_through_the_real_plugin_manager(hermes_env):
    manager, loaded = _load(hermes_env)
    assert loaded.enabled is True
    assert set(loaded.hooks_registered) == {"on_session_start", "post_tool_call", "on_session_end"}
    assert PLUGIN_KEY in manager._cli_commands


def test_real_memory_tool_change_is_recorded(hermes_env):
    manager, loaded = _load(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1", platform="cli")
    history = _history(hermes_env)
    baseline = history.head()
    assert baseline, "session start must record a baseline before anything changes"

    from tools.memory_tool import load_on_disk_store, memory_tool
    result = json.loads(memory_tool(action="add", target="memory",
                                    content="prefers concise answers", store=load_on_disk_store()))
    assert result.get("success") is not False, result
    manager.invoke_hook("post_tool_call", tool_name="memory",
                        args={"action": "add", "target": "memory", "content": "prefers concise answers"},
                        result=json.dumps(result), status="success", session_id="s1")
    assert loaded.module.WORKER.flush(timeout=30)

    newest = history.log(limit=1)[0]
    assert newest.sha != baseline
    assert newest.subject == "after memory"
    assert ("M", "memories/MEMORY.md") in newest.changes
    assert b"prefers concise answers" in history.read_blob("HEAD", "memories/MEMORY.md")
    assert b"prefers concise answers" not in history.read_blob(baseline, "memories/MEMORY.md")


def test_unrelated_tools_do_not_record(hermes_env):
    manager, loaded = _load(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1")
    history = _history(hermes_env)
    before = history.commit_count()
    (hermes_env / "SOUL.md").write_text("changed by something else", newline="\n")
    for tool, args in (("terminal", {"command": "ls"}),
                       ("write_file", {"path": "/tmp/project/app.py", "content": "x"}),
                       ("read_file", {"path": "SOUL.md"})):
        manager.invoke_hook("post_tool_call", tool_name=tool, args=args, status="success", session_id="s1")
    assert loaded.module.WORKER.flush(timeout=30)
    assert history.commit_count() == before


def test_file_tool_touching_a_tracked_path_records(hermes_env):
    manager, loaded = _load(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1")
    (hermes_env / "SOUL.md").write_text("edited through write_file", newline="\n")
    manager.invoke_hook("post_tool_call", tool_name="write_file",
                        args={"path": "~/.hermes/SOUL.md", "content": "..."}, status="success")
    assert loaded.module.WORKER.flush(timeout=30)
    history = _history(hermes_env)
    assert history.read_blob("HEAD", "SOUL.md") == b"edited through write_file"


def test_turn_end_catches_changes_made_outside_tools(hermes_env):
    manager, loaded = _load(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1")
    archived = hermes_env / "skills" / ".archive" / "notes"
    archived.parent.mkdir(parents=True)
    shutil.move(str(hermes_env / "skills" / "productivity" / "notes"), str(archived))  # curator-style move
    manager.invoke_hook("on_session_end", session_id="s1", completed=True)
    assert loaded.module.WORKER.flush(timeout=30)
    newest = _history(hermes_env).log(limit=1)[0]
    assert newest.subject == "turn end"
    assert ("A", "skills/.archive/notes/SKILL.md") in newest.changes


def test_multiplexed_profiles_keep_separate_histories(hermes_env, tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    manager, loaded = _load(hermes_env)
    other = tmp_path / "profiles" / "work"
    (other / "memories").mkdir(parents=True)
    (other / "memories" / "MEMORY.md").write_text("work profile memory\n", newline="\n")

    token = set_hermes_home_override(other)
    try:
        manager.invoke_hook("on_session_start", session_id="work-1")
        manager.invoke_hook("post_tool_call", tool_name="memory", args={}, session_id="work-1")
        assert loaded.module.WORKER.flush(timeout=30)
    finally:
        reset_hermes_home_override(token)

    work = _history(other)
    assert work.read_blob("HEAD", "memories/MEMORY.md") == b"work profile memory\n"
    default = _history(hermes_env)
    assert default.head() is None, "the launch profile must not receive the other profile's history"


def test_disabled_setting_stops_recording(hermes_env):
    manager, loaded = _load(hermes_env, settings={"enabled": False})
    manager.invoke_hook("on_session_start", session_id="s1")
    manager.invoke_hook("post_tool_call", tool_name="memory", args={}, session_id="s1")
    assert loaded.module.WORKER.flush(timeout=30)
    assert _history(hermes_env).head() is None


def _hermes_cli(home: Path, *argv: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    import hermes_cli
    root = Path(hermes_cli.__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(root), NO_COLOR="1")
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", *argv], env=env, input=input_text,
                          capture_output=True, text=True, cwd=str(home.parent), timeout=180)


def test_cli_log_and_restore_end_to_end(hermes_env):
    manager, loaded = _load(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1")
    history = _history(hermes_env)
    good = history.head()
    (hermes_env / "memories" / "MEMORY.md").write_text("", newline="\n")  # a wiped memory file
    manager.invoke_hook("on_session_end", session_id="s1")
    assert loaded.module.WORKER.flush(timeout=30)

    log = _hermes_cli(hermes_env, PLUGIN_KEY, "log", "memory")
    assert log.returncode == 0, log.stderr
    assert good[:10] in log.stdout and "turn end" in log.stdout

    refused = _hermes_cli(hermes_env, PLUGIN_KEY, "restore", good[:10], "memory")
    assert refused.returncode == 1 and "--yes" in refused.stderr
    assert (hermes_env / "memories" / "MEMORY.md").read_text() == ""

    done = _hermes_cli(hermes_env, PLUGIN_KEY, "restore", good[:10], "memory", "--yes")
    assert done.returncode == 0, done.stderr
    assert (hermes_env / "memories" / "MEMORY.md").read_text() == "first note\n"
    assert "To undo" in done.stdout

    status = _hermes_cli(hermes_env, PLUGIN_KEY, "status")
    assert status.returncode == 0 and "Versions" in status.stdout
