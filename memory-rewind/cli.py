"""`hermes memory-rewind ...` subcommands."""

from __future__ import annotations

import argparse
import sys
import time

from .gitstore import GitError, GitStore, GitUnavailable
from .restore import RestoreError, apply_restore, normalize_target, plan_restore
from .worker import WORKER, take_snapshot


def build_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="mrw_command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("status", help="Show where history lives and how much it holds")

    log = sub.add_parser("log", help="List recorded versions (newest first)")
    log.add_argument("target", nargs="?",
                     help="memory, user, soul, or a path such as skills/<category>/<skill>")
    log.add_argument("-n", "--limit", type=int, default=20, help="How many versions to show (default 20)")

    show = sub.add_parser("show", help="Print a file as it was at a version")
    show.add_argument("rev", help="Version id from `log` (or HEAD, HEAD~1, ...)")
    show.add_argument("target", help="memory, user, soul, or a file path under skills/")

    diff = sub.add_parser("diff", help="Show what changed since a version")
    diff.add_argument("rev", help="Older version id")
    diff.add_argument("target", nargs="?", help="Limit to memory, user, soul or a skills/ path")
    diff.add_argument("--to", dest="to_rev", default=None, help="Newer version id (default: current state)")
    diff.add_argument("--stat", action="store_true", help="Summary only")

    restore = sub.add_parser("restore", help="Put a file or skill back as it was at a version")
    restore.add_argument("rev", help="Version id to restore from")
    restore.add_argument("target", help="memory, user, soul, or a path under skills/")
    restore.add_argument("--yes", action="store_true", help="Apply without asking")
    restore.add_argument("--dry-run", action="store_true", help="Show the plan and stop")

    snap = sub.add_parser("snapshot", help="Record the current state now")
    snap.add_argument("-m", "--message", default="manual snapshot", help="Label for this version")

    forget = sub.add_parser("forget", help="Delete the whole history for this profile")
    forget.add_argument("--yes", action="store_true", help="Required: confirm deletion")


def run_cli(args: argparse.Namespace, ctx) -> int:
    from . import read_options, resolve_data_dir, resolve_home

    home, data_dir, options = resolve_home(), resolve_data_dir(), read_options(ctx)
    try:
        store = GitStore(home, data_dir)
    except GitUnavailable:
        print("memory-rewind needs git on PATH; install git and try again.", file=sys.stderr)
        return 2
    WORKER.flush(timeout=30)
    command = args.mrw_command
    try:
        if command == "status":
            return _status(store, home)
        if command == "snapshot":
            new = take_snapshot(home, data_dir, options, [args.message])
            print(f"Recorded {new[:10]}." if new else "Nothing changed since the last version.")
            return 0
        if command == "forget":
            if not args.yes:
                print("This deletes every recorded version for this profile. Re-run with --yes.",
                      file=sys.stderr)
                return 1
            store.destroy()
            print(f"History deleted ({store.repo}).")
            return 0
        if not store.head():
            print("No history yet. It starts with the next Hermes session, or run "
                  "`hermes memory-rewind snapshot`.")
            return 0
        if command == "log":
            paths = [normalize_target(args.target, home, options)] if args.target else []
            return _log(store, paths, args.limit)
        if command == "show":
            rev = store.resolve(args.rev)
            target = normalize_target(args.target, home, options)
            sys.stdout.buffer.write(store.read_blob(rev, target))
            sys.stdout.flush()
            return 0
        if command == "diff":
            # Record the current state first so the diff compares against what is on disk now.
            take_snapshot(home, data_dir, options, ["before diff"])
            old = store.resolve(args.rev)
            new = store.resolve(args.to_rev) if args.to_rev else store.head()
            paths = [normalize_target(args.target, home, options)] if args.target else []
            out = store.diff(old, new, paths, stat_only=args.stat)
            print(out if out.strip() else "No differences.")
            return 0
        if command == "restore":
            return _restore(store, home, data_dir, options, args)
    except (RestoreError, GitError) as exc:
        print(f"memory-rewind: {exc}", file=sys.stderr)
        return 1
    return 1


def _status(store: GitStore, home) -> int:
    head = store.head()
    print(f"Profile home : {home}")
    print(f"History      : {store.repo}")
    if not head:
        print("Versions     : none yet")
        return 0
    newest = store.log(limit=1)[0]
    print(f"Versions     : {store.commit_count()}")
    print(f"Latest       : {newest.sha[:10]}  {_when(newest.timestamp)}  {newest.subject}")
    return 0


def _log(store: GitStore, paths: list[str], limit: int) -> int:
    commits = store.log(paths, limit=limit)
    if not commits:
        print("No versions touch that path yet.")
        return 0
    for commit in commits:
        files = len(commit.changes)
        noun = "file" if files == 1 else "files"
        print(f"{commit.sha[:10]}  {_when(commit.timestamp)}  {commit.subject}  ({files} {noun})")
        for status, path in commit.changes[:8]:
            print(f"    {status}  {path}")
        if files > 8:
            print(f"    … {files - 8} more")
    return 0


def _restore(store: GitStore, home, data_dir, options, args) -> int:
    target = normalize_target(args.target, home, options)
    plan = plan_restore(store, home, args.rev, target, options)
    if plan.empty:
        print(f"{target} already matches {args.rev}; nothing to do.")
        return 0
    print(f"Restore {target} to {plan.rev[:10]}:")
    for path in plan.write:
        print(f"  write   {path}")
    for path in plan.delete:
        print(f"  remove  {path}")
    if args.dry_run:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not a terminal; re-run with --yes to apply.", file=sys.stderr)
            return 1
        if input("Apply? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    take_snapshot(home, data_dir, options, [f"before restoring {target}"])
    undo = store.head()  # the state right before this restore, recorded or already current
    apply_restore(store, home, plan)
    after = take_snapshot(home, data_dir, options, [f"restored {target} from {plan.rev[:10]}"])
    print(f"Done{f' ({after[:10]})' if after else ''}. "
          f"To undo: hermes memory-rewind restore {undo[:10]} {target}")
    if target.startswith("memories/"):
        print("Memory is loaded when a session starts; use /new (or start a new session) to see it.")
    return 0


def _when(timestamp: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
