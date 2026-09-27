# memory-rewind

Automatic version history for a Hermes agent's own knowledge — `memories/MEMORY.md`,
`memories/USER.md`, `SOUL.md` and the `skills/` tree (including curator-archived
skills) — with commands to browse, diff and restore any past version.

Hermes rewrites these files while it works: the `memory` tool edits the memory store,
`skill_manage` creates and edits skills, the curator archives and consolidates them.
None of those writes keeps a history, so a truncated `MEMORY.md`, a skill edited into
a broken state, or an archived skill that will not come back has nothing to roll back
to. memory-rewind records every change as it happens and lets you put any file or
skill back exactly as it was.

## Install

```bash
hermes plugins install AhmetArif0/hermes-memory-rewind#memory-rewind --enable
```

Requires Hermes **0.21.5 or newer** and `git` on `PATH`. Restart a running gateway
(`hermes gateway restart`) so it loads the plugin. History starts with the next session.

## Use

```bash
hermes memory-rewind log                        # recent versions of everything
hermes memory-rewind log memory                 # versions of memories/MEMORY.md
hermes memory-rewind log skills/research/arxiv  # versions of one skill
hermes memory-rewind show <version> user        # memories/USER.md at a version
hermes memory-rewind diff <version> memory      # what changed since a version
hermes memory-rewind restore <version> memory   # put MEMORY.md back (asks first)
hermes memory-rewind restore <version> skills/research/arxiv --yes
hermes memory-rewind snapshot -m "before cleanup"
hermes memory-rewind status
hermes memory-rewind forget --yes               # delete the whole history
```

`memory`, `user` and `soul` are shortcuts for `memories/MEMORY.md`,
`memories/USER.md` and `SOUL.md`. Versions are the ids printed by `log`
(`HEAD`, `HEAD~1`, … also work). Commands act on the active profile
(`hermes -p <profile> memory-rewind …`).

`log` shows what produced each version:

```text
$ hermes memory-rewind log user
3f2a9c1b7e  2026-09-27 14:05  after memory  (1 file)
    via   memory: remove, add (user)
    from  telegram, session 20260927_140012_ab12cd
    M  memories/USER.md
```

`via` names the tool call: the tool, its actions, and the memory target or skill
names, with `[error]` (or `[blocked]`, `[timeout]`, …) when the call did not succeed.
`from` names the session and, when Hermes reported it, the platform.

Every restore records the current state first and prints the command that undoes it.
Built-in memory is loaded when a session starts, so a restored `MEMORY.md`/`USER.md`
takes effect in the next session (`/new`).

## What is recorded

| Recorded | Never recorded |
|---|---|
| `memories/MEMORY.md`, `memories/USER.md` | `.env`, `auth.json`, keys and certificates, database files |
| `SOUL.md` | `skills/.hub/` (hub lock, audit log, cache, quarantined skills) |
| `skills/**`, including `skills/.archive/` | `skills/.usage.json`, curator ledger, state and backups, bundled-skill manifest |
| | Hermes lock files (`skills/.locks/`, `skills/.usage.json.lock`) |
| | symlinks, embedded git repositories, caches, files larger than `max_file_kb` |

A version is recorded when a session starts (a baseline before anything can change),
after each `memory` or `skill_manage` call (and file edits that target these paths),
and at the end of each turn to catch changes made outside tools (curator runs, hub
installs, terminal edits). Unchanged state records nothing. Each version notes the
tool calls and sessions behind it; the file list shows what actually changed.

History is a bare git repository at
`~/.hermes/plugin-data/memory-rewind/history.git` (per profile). It is isolated from
your own git setup: your global config, hooks and signing are never used.

## Settings

Set in the Desktop **Plugins** tab, or in `config.yaml`:

```yaml
plugins:
  entries:
    memory-rewind:
      settings:
        enabled: true        # record new versions automatically
        track_skills: true   # version skills/ (including .archive)
        track_soul: true     # version SOUL.md
        max_file_kb: 1024    # skip larger files
```

## Security and footprint

- **No network access**, no downloads, no self-update, and no Python dependencies
  (standard library only). The only external program is the local `git` binary.
- **What it registers:** the hooks `on_session_start`, `post_tool_call` and
  `on_session_end`, and the `hermes memory-rewind` command. It gives the model no
  tools and adds no slash commands. `register()` only registers; it does no I/O.
- **What it reads:** `memories/MEMORY.md`, `memories/USER.md`, `SOUL.md` and `skills/`
  (minus the exclusions above), plus its own settings. It never opens `.env`,
  `auth.json`, databases or `skills/.hub/`. From hook payloads it keeps only the tool
  name, action names, memory target, skill names, call outcome, session id and platform,
  never message or file content.
- **What it writes:** only `$HERMES_HOME/plugin-data/memory-rewind/` (the history
  repository and an index cache). Tracked files are written only when you run
  `restore`, and `forget` deletes only the history.
- **How git runs:** against its own bare repository, with your global and system git
  config, hooks, signing and pager ignored, literal pathspecs, a 60-second timeout,
  and background `gc` detaching disabled.
- **When it runs:** a synchronous baseline when a session starts (about 35 ms once
  history exists; the very first one records the whole skills tree and took 0.7 s on a
  1,100-file tree), and background snapshots after
  `memory`/`skill_manage` calls and at the end of each turn. On exit, a queued
  snapshot gets up to 5 seconds to finish.

## Privacy

History keeps what the files contained. If you ask the agent to forget something,
the removal is recorded, but older versions still contain it. Versions also note session
ids, platforms and skill names. Run `hermes memory-rewind forget --yes` to delete the
history for the active profile.

## Limits

- External memory providers (Honcho, Mem0, …) store data outside these files and are
  not covered.
- Restore is a command you run; the agent has no tool to rewrite history or restore.

## How it works

See [docs/DESIGN.md](docs/DESIGN.md).

## Development

```bash
python -m pytest tests/                                   # unit tests (no Hermes needed)
PYTHONPATH=/path/to/hermes-agent python -m pytest tests/  # plus end-to-end tests against Hermes
```

The end-to-end tests load the plugin through Hermes' real plugin manager, drive the real
memory tool and hook dispatch, run the `hermes` CLI, and check that separate profiles
keep separate histories.

## Changes

- **1.1.0**: `log` shows what produced each version: the tool call (tool, actions,
  memory target or skill names, and the outcome when it did not succeed) and the
  session and platform. Hermes' skill lock files and curator/Termux state files are no
  longer recorded. Before, every `skill_manage` call added a version holding only a
  lock file. Older versions that contain them never restore them. After upgrading,
  the first new version lists them as removed from the history; the files on disk
  are not touched.
- **1.0.0**: first release.

## License

MIT
