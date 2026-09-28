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
import time
from pathlib import Path

import pytest

hermes_plugins = pytest.importorskip("hermes_cli.plugins")
try:  # newer Hermes ships hermes_yaml instead of depending on PyYAML
    import hermes_yaml as yaml
except ImportError:
    yaml = pytest.importorskip("yaml")

from conftest import PLUGIN_DIR  # noqa: E402

PLUGIN_KEY = "memory-rewind"


def _install(home: Path, settings: dict | None = None, config: dict | None = None) -> None:
    shutil.copytree(PLUGIN_DIR, home / "plugins" / PLUGIN_KEY,
                    ignore=shutil.ignore_patterns("__pycache__"))
    cfg = {**(config or {}), "plugins": {"enabled": [PLUGIN_KEY]}}
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


def _load(home: Path, settings: dict | None = None, config: dict | None = None):
    _install(home, settings, config)
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
    assert set(loaded.hooks_registered) == {"on_session_start", "pre_llm_call", "post_tool_call", "on_session_end"}
    import inspect
    assert [list(inspect.signature(cb).parameters) for cb in manager._hooks["pre_llm_call"]] == \
        [["session_id", "platform"]], "Hermes passes only declared fields: never ask for the user message"
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


def test_write_approved_between_turns_is_not_credited_to_the_next_turn(hermes_env):
    """With memory.write_approval on, the agent's writes are staged and `/memory approve`
    applies one between turns, outside any hook. The next turn's calls change nothing, so no
    version may name them or the session for the approved write."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from memory_rewind.provenance import parse_body
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store, memory_tool
    manager, loaded = _load(hermes_env, config={"memory": {"write_approval": True}})
    history = _history(hermes_env)
    memory = hermes_env / "memories" / "MEMORY.md"

    def agent_memory_call(operations):  # what the agent loop does: run the tool, then post_tool_call
        result = memory_tool(action="", target="memory", operations=operations, store=load_on_disk_store())
        manager.invoke_hook("post_tool_call", tool_name="memory", result=result, status="ok",
                            args={"target": "memory", "operations": operations}, session_id="s1")
        assert loaded.module.WORKER.flush(timeout=30)
        return json.loads(result)

    manager.invoke_hook("on_session_start", session_id="s1", platform="telegram")
    assert manager.invoke_hook("pre_llm_call", session_id="s1", platform="telegram", user_message="hi",
                               conversation_history=[]) == [], "the plugin must inject nothing"
    staged = agent_memory_call([{"action": "add", "content": "approved later"}])
    assert staged.get("staged") is True, staged
    manager.invoke_hook("on_session_end", session_id="s1", completed=True, platform="telegram")
    assert loaded.module.WORKER.flush(timeout=30)
    before = history.commit_count()

    handle_pending_subcommand(wa.MEMORY, ["approve", staged["pending_id"]], memory_store=load_on_disk_store())
    assert "approved later" in memory.read_text(encoding="utf-8")

    manager.invoke_hook("pre_llm_call", session_id="s1", platform="telegram")
    assert agent_memory_call([{"action": "remove", "old_text": "first note"}]).get("staged") is True
    manager.invoke_hook("on_session_end", session_id="s1", completed=True, platform="telegram")
    assert loaded.module.WORKER.flush(timeout=30)

    assert history.commit_count() == before + 1, "only the turn-start baseline is new"
    newest = history.log(limit=1)[0]
    assert newest.subject == "turn start" and ("M", "memories/MEMORY.md") in newest.changes
    assert parse_body(newest.body).calls == [] and parse_body(newest.body).sessions == []
    assert b"approved later" in history.read_blob("HEAD", "memories/MEMORY.md")


def test_skill_archived_between_sessions_is_not_credited_to_the_next_one(hermes_env):
    """The curator archives a skill while no session runs (no tool call, no turn); the next
    session's baseline records the move without naming that session."""
    from memory_rewind.provenance import parse_body
    from tools import skill_usage
    manager, loaded = _load(hermes_env)
    history = _history(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1", platform="telegram")

    skill_usage.mark_agent_created("notes")  # curator-managed, like a skill the agent wrote
    archived, message = skill_usage.archive_skill("notes")
    assert archived, message

    manager.invoke_hook("on_session_start", session_id="s2", platform="discord")
    newest = history.log(limit=1)[0]
    assert newest.subject == "session start"
    assert ("D", "skills/productivity/notes/SKILL.md") in newest.changes
    assert ("A", "skills/.archive/notes/SKILL.md") in newest.changes
    assert parse_body(newest.body).sessions == []


def test_turn_start_baseline_lets_queued_versions_land_first(hermes_env, monkeypatch):
    """A call's version still queued when the next turn starts keeps its change and its call."""
    from memory_rewind.provenance import parse_body
    manager, loaded = _load(hermes_env)
    history = _history(hermes_env)
    worker = sys.modules[loaded.module.__name__ + ".worker"]
    real_snapshot = worker.take_snapshot
    monkeypatch.setattr(worker, "take_snapshot", lambda *a, **k: time.sleep(0.5) or real_snapshot(*a, **k))

    manager.invoke_hook("on_session_start", session_id="s1", platform="cli")
    (hermes_env / "memories" / "MEMORY.md").write_text("first note\nqueued\n", newline="\n")
    manager.invoke_hook("post_tool_call", tool_name="memory", status="ok", session_id="s1",
                        args={"target": "memory", "operations": [{"action": "add", "content": "queued"}]})
    manager.invoke_hook("pre_llm_call", session_id="s1", platform="cli")
    assert loaded.module.WORKER.flush(timeout=30)

    newest = history.log(limit=1)[0]
    assert newest.subject == "after memory"
    assert parse_body(newest.body).calls == ["memory: add (memory)"]


def test_resumed_session_platform_is_learned_from_turn_hooks(hermes_env):
    """A session resumed in a fresh process gets no on_session_start: its platform is unknown
    (never guessed) until a turn starts or ends, and known for the tool calls after that."""
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

    manager.invoke_hook("pre_llm_call", session_id="resumed-2", platform="slack")
    memory.write_text("first note\nsecond\nthird\nfourth\n", newline="\n")
    manager.invoke_hook("post_tool_call", tool_name="memory", args=args, status="ok", session_id="resumed-2")
    assert loaded.module.WORKER.flush(timeout=30)
    assert parse_body(history.log(limit=1)[0].body).sessions == [("resumed-2", "slack")]


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
        (other / "memories" / "MEMORY.md").write_text("work profile memory\nedited\n", newline="\n")
        manager.invoke_hook("pre_llm_call", session_id="work-1", platform="telegram")
    finally:
        reset_hermes_home_override(token)

    work = _history(other)
    assert work.read_blob("HEAD", "memories/MEMORY.md") == b"work profile memory\nedited\n"
    assert work.log(limit=1)[0].subject == "turn start"
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
        worker = manager._plugins[PLUGIN_KEY].module.WORKER
        manager.invoke_hook("on_session_start", session_id="home-1", platform="cli")
        (hermes_env / "memories" / "MEMORY.md").write_text("first note\nhome edit\n", newline="\n")
        manager.invoke_hook("on_session_end", session_id="home-1", completed=True, platform="cli")

        work = tmp_path / "profiles" / "work"
        (work / "memories").mkdir(parents=True)
        (work / "memories" / "MEMORY.md").write_text("work profile memory\n", newline="\n")
        token = set_hermes_home_override(work)
        try:
            manager.invoke_hook("on_session_start", session_id="work-1", platform="telegram")
            (work / "memories" / "MEMORY.md").write_text("work profile memory\nwork edit\n", newline="\n")
            manager.invoke_hook("on_session_end", session_id="work-1", completed=True, platform="telegram")
            work_context = contextvars.copy_context()
        finally:
            reset_hermes_home_override(token)
        assert worker.flush(timeout=30)

        with ThreadPoolExecutor(max_workers=1) as pool:
            work_view = pool.submit(work_context.run, handler, "").result()
            home_view = pool.submit(contextvars.copy_context().run, handler, "").result()
            work_rev = _history(work).head()
            version_view = pool.submit(work_context.run, handler, work_rev[:10]).result()

        assert "from  telegram, session work-1" in work_view and "home-1" not in work_view
        assert "from  cli, session home-1" in home_view and "work-1" not in home_view
        assert "+work edit" in version_view
        assert hermes_plugins_mod.resolve_plugin_command_result(handler("help")).startswith("/memory-history")
        assert _history(work).commit_count() == 2 and _history(hermes_env).commit_count() == 2, \
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
    assert "          brings back: first note\n" in refused.stdout, refused.stdout
    assert (hermes_env / "memories" / "MEMORY.md").read_text() == ""

    done = _hermes_cli(hermes_env, PLUGIN_KEY, "restore", good[:10], "memory", "--yes")
    assert done.returncode == 0, done.stderr
    assert (hermes_env / "memories" / "MEMORY.md").read_text() == "first note\n"
    assert "To undo" in done.stdout

    status = _hermes_cli(hermes_env, PLUGIN_KEY, "status")
    assert status.returncode == 0 and "Versions" in status.stdout


def test_history_survives_hermes_import_of_an_older_backup(hermes_env, tmp_path):
    """`hermes import` puts the backup's copy of the history back over the live one; the versions
    recorded after the backup, the state right before the import included, must stay."""
    manager, loaded = _load(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1", platform="cli")
    archive = tmp_path / "hermes-backup.zip"
    made = _hermes_cli(hermes_env, "backup", "-o", str(archive))
    assert made.returncode == 0, made.stderr

    memory = hermes_env / "memories" / "MEMORY.md"
    memory.write_text("first note\n§\nwritten after the backup", encoding="utf-8", newline="\n")
    manager.invoke_hook("post_tool_call", tool_name="memory", status="ok", session_id="s1",
                        args={"target": "memory", "action": "add", "content": "written after the backup"})
    assert loaded.module.WORKER.flush(timeout=30)
    later = _history(hermes_env).head()

    imported = _hermes_cli(hermes_env, "import", str(archive), "--force")
    assert imported.returncode == 0, imported.stderr
    assert memory.read_text() == "first note\n", "the import put the backup's memory back"
    log = _hermes_cli(hermes_env, PLUGIN_KEY, "log", "memory")
    assert log.returncode == 0 and later[:10] in log.stdout, log.stdout

    manager.invoke_hook("pre_llm_call", session_id="s2", platform="cli")
    history = _history(hermes_env)
    assert history._is_ancestor(later, history.head())
    undo = _hermes_cli(hermes_env, PLUGIN_KEY, "restore", later[:10], "memory", "--yes")
    assert undo.returncode == 0, undo.stderr
    assert memory.read_text(encoding="utf-8") == "first note\n§\nwritten after the backup"


def test_restore_never_overwrites_a_live_agents_memory_write(hermes_env):
    """A memory write in flight (the memory tool holds its lock and has read the file) when the
    user applies a restore: the restore waits for it, sees the file changed since its plan, and
    refuses instead of silently replacing what the agent just stored."""
    import threading
    from memory_rewind.restore import RestoreError, apply_restore, plan_restore
    from memory_rewind.tracking import TrackingOptions
    from tools.memory_tool import load_on_disk_store

    manager, loaded = _load(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1", platform="cli")
    history = _history(hermes_env)
    good = history.head()
    memory = hermes_env / "memories" / "MEMORY.md"
    memory.write_text("", newline="\n")
    manager.invoke_hook("pre_llm_call", session_id="s1", platform="cli")
    plan = plan_restore(history, hermes_env, good, "memories/MEMORY.md", TrackingOptions())

    read_done, finish = threading.Event(), threading.Event()

    def agent_write():
        def apply(entries, _limit):  # runs under the memory tool's lock, after its re-read
            read_done.set()
            finish.wait(5)
            return entries + ["Agent learned X"], "added"
        assert load_on_disk_store()._mutate("memory", apply).get("success")

    agent = threading.Thread(target=agent_write)
    agent.start()
    assert read_done.wait(5)
    threading.Timer(0.5, finish.set).start()
    with pytest.raises(RestoreError, match="changed after the restore plan was made"):
        apply_restore(history, hermes_env, plan)
    agent.join()
    assert memory.read_text() == "Agent learned X", "the agent's write is kept, not replaced"


def test_restore_plan_names_the_entries_it_brings_back_and_removes(hermes_env):
    """Bringing back one deleted memory entry means restoring the whole file, which also removes
    entries added since. The plan must say so before anything is written."""
    from tools.memory_tool import load_on_disk_store

    manager, loaded = _load(hermes_env)
    manager.invoke_hook("on_session_start", session_id="s1", platform="cli")
    store = load_on_disk_store()
    assert store.add("memory", "Project Atlas uses Postgres 16").get("success")
    manager.invoke_hook("pre_llm_call", session_id="s1", platform="cli")
    before_delete = _history(hermes_env).head()
    assert store.remove("memory", "Postgres").get("success")  # what the Star Map's delete does
    assert store.add("memory", "Editor: Helix").get("success")
    manager.invoke_hook("pre_llm_call", session_id="s1", platform="cli")

    plan = _hermes_cli(hermes_env, PLUGIN_KEY, "restore", before_delete[:10], "memory", "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert "  write   memories/MEMORY.md\n" in plan.stdout
    assert "          brings back: Project Atlas uses Postgres 16\n" in plan.stdout, plan.stdout
    assert "          removes:     Editor: Helix\n" in plan.stdout, plan.stdout
    assert "first note" not in plan.stdout, "entries kept on both sides are not listed"
