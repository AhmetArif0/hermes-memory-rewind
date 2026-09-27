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
    assert "memory-history" in manager._plugin_commands, "the chat command must not collide with a built-in"


def test_real_memory_tool_change_is_recorded(hermes_env):
    manager, loaded = _load(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1", platform="cli")
    history = _history(hermes_env)
    baseline = history.head()
    assert baseline, "session start must record a baseline before anything changes"

    from tools.memory_tool import load_on_disk_store, memory_tool
    # The batch shape the memory tool's schema asks the model to use.
    args = {"target": "memory", "operations": [{"action": "add", "content": "prefers concise answers"}]}
    result = json.loads(memory_tool(action="", target=args["target"], operations=args["operations"],
                                    store=load_on_disk_store()))
    assert result.get("success") is not False, result
    manager.invoke_hook("post_tool_call", tool_name="memory", args=args,
                        result=json.dumps(result), status="ok", session_id="s1")
    assert loaded.module.WORKER.flush(timeout=30)

    newest = history.log(limit=1)[0]
    assert newest.sha != baseline
    assert newest.subject == "after memory"
    assert ("M", "memories/MEMORY.md") in newest.changes
    assert b"prefers concise answers" in history.read_blob("HEAD", "memories/MEMORY.md")
    assert b"prefers concise answers" not in history.read_blob(baseline, "memories/MEMORY.md")
    from memory_rewind.provenance import parse_body
    origin = parse_body(newest.body)
    assert origin.calls == ["memory: add (memory)"]
    assert origin.sessions == [("s1", "cli")], "platform comes from the session's on_session_start"
    assert b"prefers concise answers" not in newest.body.encode()


def test_real_tool_dispatch_provenance(hermes_env, monkeypatch):
    """skill_manage through Hermes' own dispatcher and global hook bus: the args, status and
    session id the plugin sees are the ones Hermes really sends."""
    from hermes_cli import plugins as hermes_plugins_mod
    from hermes_cli.lifecycle import invoke_hook

    _install(hermes_env)
    hermes_plugins_mod._reset_plugin_managers_for_tests()
    try:
        hermes_plugins_mod.discover_plugins(force=True)
        loaded = hermes_plugins_mod.get_plugin_manager()._plugins[PLUGIN_KEY]
        assert loaded.error is None, loaded.error
        described = []
        real_describe = loaded.module.describe_call
        monkeypatch.setattr(loaded.module, "describe_call",
                            lambda *a: described.append(real_describe(*a)) or described[-1])

        import tools.skill_manager_tool  # noqa: F401  (registers skill_manage)
        from model_tools import handle_function_call

        invoke_hook("on_session_start", session_id="s1", model="test-model", platform="telegram")
        history = _history(hermes_env)
        before = history.commit_count()
        created = json.loads(handle_function_call("skill_manage", {"operations": [{
            "action": "create", "name": "provenance-demo",
            "content": "---\nname: provenance-demo\ndescription: Demo.\n---\nSteps.\n"}]},
            task_id="t1", session_id="s1"))
        assert created.get("success") is True, created
        assert loaded.module.WORKER.flush(timeout=30)

        newest = history.log(limit=1)[0]
        assert history.commit_count() == before + 1 and newest.subject == "after skill_manage"
        assert any(path.endswith("provenance-demo/SKILL.md") for _, path in newest.changes)
        from memory_rewind.provenance import parse_body
        origin = parse_body(newest.body)
        assert origin.calls == ["skill_manage: create provenance-demo"]
        assert origin.sessions == [("s1", "telegram")]

        failed = json.loads(handle_function_call("skill_manage", {"operations": [{
            "action": "patch", "name": "no-such-skill", "old_string": "a", "new_string": "b"}]},
            task_id="t1", session_id="s1"))
        assert failed.get("success") is not True, failed
        assert loaded.module.WORKER.flush(timeout=30)
        assert described[-1] == "skill_manage: patch no-such-skill [error]"
        assert history.commit_count() == before + 1, "a failed call changes nothing, so no version"
    finally:
        hermes_plugins_mod._reset_plugin_managers_for_tests()


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
    manager.invoke_hook("on_session_end", session_id="s1", completed=True, platform="discord")
    assert loaded.module.WORKER.flush(timeout=30)
    newest = _history(hermes_env).log(limit=1)[0]
    assert newest.subject == "turn end"
    assert ("A", "skills/.archive/notes/SKILL.md") in newest.changes
    from memory_rewind.provenance import parse_body
    origin = parse_body(newest.body)
    assert origin.calls == [] and origin.sessions == [("s1", "discord")]


def test_resumed_session_platform_is_learned_at_turn_end(hermes_env):
    """A session resumed in a fresh process gets no on_session_start: its platform is unknown
    (never guessed) until a turn ends, and known for the tool calls after that."""
    from memory_rewind.provenance import parse_body
    manager, loaded = _load(hermes_env)
    history = _history(hermes_env)
    memory = hermes_env / "memories" / "MEMORY.md"
    args = {"target": "memory", "operations": [{"action": "add", "content": "x"}]}

    memory.write_text("first note\nsecond\n", newline="\n")
    manager.invoke_hook("post_tool_call", tool_name="memory", args=args, status="ok", session_id="resumed")
    assert loaded.module.WORKER.flush(timeout=30)
    assert parse_body(history.log(limit=1)[0].body).sessions == [("resumed", "")]

    manager.invoke_hook("on_session_end", session_id="resumed", completed=True, platform="telegram")
    assert loaded.module.WORKER.flush(timeout=30)
    memory.write_text("first note\nsecond\nthird\n", newline="\n")
    manager.invoke_hook("post_tool_call", tool_name="memory", args=args, status="ok", session_id="resumed")
    assert loaded.module.WORKER.flush(timeout=30)
    assert parse_body(history.log(limit=1)[0].body).sessions == [("resumed", "telegram")]


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


def test_memory_history_chat_command_follows_the_profile(hermes_env, tmp_path):
    """`/memory-history` through Hermes' own command lookup (the one CLI, gateway and TUI use). The
    gateway runs sync handlers on a pool thread with the caller's context copied, which is how a
    multiplexed profile reaches the handler: each profile must see only its own history."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from hermes_cli import plugins as hermes_plugins_mod
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _install(hermes_env)
    hermes_plugins_mod._reset_plugin_managers_for_tests()
    try:
        hermes_plugins_mod.discover_plugins(force=True)
        manager = hermes_plugins_mod.get_plugin_manager()
        handler = hermes_plugins_mod.get_plugin_command_handler("memory-history")
        assert handler is not None
        manager.invoke_hook("on_session_start", session_id="home-1", platform="cli")

        work = tmp_path / "profiles" / "work"
        (work / "memories").mkdir(parents=True)
        (work / "memories" / "MEMORY.md").write_text("work profile memory\n", newline="\n")
        token = set_hermes_home_override(work)
        try:
            manager.invoke_hook("on_session_start", session_id="work-1", platform="telegram")
            work_context = contextvars.copy_context()
        finally:
            reset_hermes_home_override(token)

        with ThreadPoolExecutor(max_workers=1) as pool:
            work_view = pool.submit(work_context.run, handler, "").result()
            home_view = pool.submit(contextvars.copy_context().run, handler, "").result()
            work_rev = _history(work).head()
            version_view = pool.submit(work_context.run, handler, work_rev[:10]).result()

        assert "from  telegram, session work-1" in work_view and "home-1" not in work_view
        assert "from  cli, session home-1" in home_view and "work-1" not in home_view
        assert "+work profile memory" in version_view
        assert hermes_plugins_mod.resolve_plugin_command_result(handler("help")).startswith("/memory-history")
        assert _history(work).commit_count() == 1 and _history(hermes_env).commit_count() == 1, \
            "viewing must not record a version"
    finally:
        hermes_plugins_mod._reset_plugin_managers_for_tests()


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
    manager.invoke_hook("on_session_start", session_id="s1", platform="cli")
    history = _history(hermes_env)
    good = history.head()
    (hermes_env / "memories" / "MEMORY.md").write_text("", newline="\n")  # a wiped memory file
    manager.invoke_hook("post_tool_call", tool_name="memory", status="ok", session_id="s1",
                        args={"target": "memory", "operations": [{"action": "remove", "old_text": "first"}]})
    assert loaded.module.WORKER.flush(timeout=30)

    log = _hermes_cli(hermes_env, PLUGIN_KEY, "log", "memory")
    assert log.returncode == 0, log.stderr
    assert good[:10] in log.stdout and "after memory" in log.stdout
    assert "    via   memory: remove (memory)\n    from  cli, session s1\n" in log.stdout

    refused = _hermes_cli(hermes_env, PLUGIN_KEY, "restore", good[:10], "memory")
    assert refused.returncode == 1 and "--yes" in refused.stderr
    assert (hermes_env / "memories" / "MEMORY.md").read_text() == ""

    done = _hermes_cli(hermes_env, PLUGIN_KEY, "restore", good[:10], "memory", "--yes")
    assert done.returncode == 0, done.stderr
    assert (hermes_env / "memories" / "MEMORY.md").read_text() == "first note\n"
    assert "To undo" in done.stdout

    status = _hermes_cli(hermes_env, PLUGIN_KEY, "status")
    assert status.returncode == 0 and "Versions" in status.stdout
