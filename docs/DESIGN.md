# memory-rewind — design notes

## Problem

Hermes rewrites its own knowledge while it runs: the `memory` tool edits
`memories/MEMORY.md` and `memories/USER.md`, `skill_manage` creates and edits
skills, the curator archives and consolidates them, and skill-hub installs add
more. None of those paths keeps a per-change history. When a write goes wrong
(a truncated `MEMORY.md`, a skill edited into a broken state, an archived skill
that will not come back), there is nothing to diff against and nothing to roll
back to. `/snapshot` and `hermes backup` take whole-home copies on demand, the
curator can roll back only its own runs, and checkpoints cover project
directories, not the agent's home.

memory-rewind keeps an automatic, per-change version history of exactly those
files, and lets the user browse, diff and restore any past version.

## What is tracked

Relative to the active profile's `HERMES_HOME`:

| Path | Why |
|---|---|
| `memories/MEMORY.md`, `memories/USER.md` | built-in memory store |
| `SOUL.md` | persona file |
| `skills/**` (including `skills/.archive/`) | active and curator-archived skills |

Never tracked, anywhere under those roots:

- Hermes bookkeeping that churns on every read or is itself a backup:
  `skills/.usage.json`, `skills/.curator_ledger.jsonl`, `skills/.bundled_manifest`,
  `skills/.curator_suppressed*`, `skills/.curator_backups/`.
- `skills/.hub/`: the hub lock file, audit log, index cache and **quarantined**
  skills. Quarantined content must never become restorable.
- Secret-shaped files: `.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`,
  `id_rsa*`, `id_ecdsa*`, `id_ed25519*`, `*.keystore`, `auth.json`,
  `credentials*.json`, and database files (`*.db`, `*.db-wal`, `*.db-shm`,
  `*.sqlite*`).
- Tooling noise: `.git/`, `__pycache__/`, `node_modules/`, virtualenvs, caches,
  `.DS_Store`.
- Symlinks (files and directories), directories that are themselves git
  repositories, and files larger than `max_file_kb` (default 1024 KiB).

## Storage

History lives in a bare git repository at
`<HERMES_HOME>/plugin-data/memory-rewind/history.git`, resolved through
`plugins.plugin_storage.plugin_data_dir`. It follows the active profile, never
touches the skills tree, and survives `hermes plugins update`/`remove`.

Every git call runs with the same isolation Hermes uses for its own shadow
checkpoints: `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` set explicitly,
inherited `GIT_*` routing variables cleared, global and system git config
ignored (`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` → null device), signing off,
literal pathspecs, a timeout, and no console window on Windows. The user's own
git configuration, hooks and signing setup are never involved.

## Snapshots

A snapshot computes the tracked file set in Python, stages it into a private
index, writes a tree, and commits only when the tree changed.

- Concurrency: several Hermes processes can share a profile (gateway, CLI,
  kanban workers). Each snapshot stages into its own temporary copy of a cached
  index and publishes with a compare-and-swap `git update-ref` against the head
  it started from; on a lost race it retries. The cached index is only a
  stat-cache accelerator; correctness never depends on it. No lock files.
- Cost on a realistic home (1,119 skill files, 15 MB): about 0.7 s for the
  first snapshot, about 35 ms afterwards.

Triggers:

| Hook | Mode | Reason |
|---|---|---|
| `on_session_start` | synchronous | baseline before the session can change anything |
| `post_tool_call` for `memory`, `skill_manage`, and `write_file`/`patch` targeting a tracked path | background | capture each change as it happens |
| `on_session_end` (each turn) | background | catch writes that do not go through a tool (curator, hub installs, terminal edits) |

Hook callbacks resolve `HERMES_HOME` and the data directory in the caller's
context (Hermes copies the profile context into bounded hook workers), then
hand plain paths to a per-process background thread. The agent loop never waits
on git beyond the ~35 ms session-start baseline. Requests for the same profile
are coalesced, and an `atexit` handler gives queued snapshots up to 5 s to land
before a one-shot process (`hermes chat -q … --oneshot`) exits; without it the
turn-end snapshot of such a run is lost.

Hermes syncs its bundled skills into `skills/`, so the first snapshot of a
profile contains them too, and later versions show what `hermes update`
changed in them.

## Restore

Restore is a user command (`hermes memory-rewind restore`), never an agent
tool. It:

1. accepts only tracked targets (a memory file, `SOUL.md`, a path under
   `skills/`), rejecting absolute paths, `..`, excluded names and symlinked
   parents;
2. shows what will change and requires `--yes` (or an interactive "y");
3. snapshots the current state first, so every restore can itself be undone;
4. writes each file atomically (temp file in the same directory, then
   `os.replace`), restores the executable bit, and removes files under the
   target that did not exist at the chosen revision;
5. snapshots again, recording what was restored.

This matches how Hermes' own `hermes backup` import, `/snapshot` restore and
curator rollback put the user's own bytes back.

Built-in memory is loaded into the system prompt as a frozen snapshot at
session start, so a restored `MEMORY.md`/`USER.md` takes effect in the next
session (`/new`).

## Privacy

History keeps what the files contained. If the agent is asked to forget
something, the removal is recorded but older versions still contain it.
`hermes memory-rewind forget --yes` deletes the whole history for the active
profile.

## Not covered

External memory providers (Honcho, Mem0, …) store data outside these files and
are out of scope.

## Verification

- Unit tests (tracking rules, git isolation against a hostile user git setup,
  compare-and-swap under a lost race, literal pathspecs, restore semantics,
  symlink refusal, exit flush) run without Hermes.
- End-to-end tests load the plugin through Hermes' real `PluginManager`, drive
  the real `memory` tool and hook dispatch, check multiplexed profiles keep
  separate histories, and run `hermes memory-rewind log/restore/status` as a
  subprocess. They run against the minimum supported release (v0.21.5) and
  `main`.
- Each safety property is mutation-checked: disabling it makes at least one
  test fail.
- `hermes plugins validate` (including the install-time security scan) and
  `hermes plugins doctor --ci` pass.
