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

Every restore records the current state first and prints the command that undoes it.
Built-in memory is loaded when a session starts, so a restored `MEMORY.md`/`USER.md`
takes effect in the next session (`/new`).

## What is recorded

| Recorded | Never recorded |
|---|---|
| `memories/MEMORY.md`, `memories/USER.md` | `.env`, `auth.json`, keys and certificates, database files |
| `SOUL.md` | `skills/.hub/` (hub lock, audit log, cache, quarantined skills) |
| `skills/**`, including `skills/.archive/` | `skills/.usage.json`, curator ledger and backups, bundled-skill manifest |
| | symlinks, embedded git repositories, caches, files larger than `max_file_kb` |

A version is recorded when a session starts (a baseline before anything can change),
after each `memory` or `skill_manage` call (and file edits that target these paths),
and at the end of each turn to catch changes made outside tools (curator runs, hub
installs, terminal edits). Unchanged state records nothing.

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

## Privacy

History keeps what the files contained. If you ask the agent to forget something,
the removal is recorded, but older versions still contain it. Run
`hermes memory-rewind forget --yes` to delete the history for the active profile.

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

## License

MIT
