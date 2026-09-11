#!/usr/bin/env python3
"""
rmjson.py - delete tracked files one at a time, one commit per deletion,
then push once at the end.

    python rmjson.py traces/                     # dry run is NOT default
    python rmjson.py traces/ --dry-run           # list what would go
    python rmjson.py traces/ --no-push           # keep it local

Only touches files git already tracks. Interrupted runs resume cleanly --
just run the same command again.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

DEFAULT_MESSAGE = "Remove Traces Logs"


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed:\n"
                       f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "replace")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", type=Path,
                   help="directory (or single file) to remove from")
    p.add_argument("--pattern", default="*.json",
                   help="glob to match, relative to path (default: *.json)")
    p.add_argument("--message", default=DEFAULT_MESSAGE,
                   help=f"commit message (default: {DEFAULT_MESSAGE!r}); "
                        "fields: name, path, i, n")
    p.add_argument("--reverse", action="store_true",
                   help="delete in reverse filename order")
    p.add_argument("--keep-dir", dest="keep_dir", action="store_true",
                   default=True,
                   help="commit a placeholder into any directory that would "
                        "otherwise be emptied, so it survives (default)")
    p.add_argument("--no-keep-dir", dest="keep_dir", action="store_false",
                   help="let emptied directories disappear from the repo")
    p.add_argument("--keep-file", default=".gitkeep",
                   help="placeholder filename (default: .gitkeep)")
    p.add_argument("--keep-message", default="Keep {dir} directory",
                   help="commit message for the placeholder; field: dir")
    p.add_argument("--limit", type=int, default=None,
                   help="stop after this many deletions")
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--push", dest="push", action="store_true", default=True)
    p.add_argument("--no-push", dest="push", action="store_false")
    p.add_argument("--remote", default="origin")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    args = p.parse_args()

    target = args.path.expanduser().resolve()
    anchor = target if target.is_dir() else target.parent
    if not anchor.exists():
        return fail(f"no such path: {target}")

    try:
        repo = Path(git(anchor, "rev-parse", "--show-toplevel").strip())
    except GitError:
        return fail(f"{anchor} is not inside a git repository")

    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == "HEAD":
        return fail("detached HEAD; check out a branch first")

    # only tracked files -- git rm would fail on anything else
    rel = target.relative_to(repo).as_posix()
    spec = f"{rel}/{args.pattern}" if target.is_dir() else rel
    out = git(repo, "ls-files", "-z", "--", spec)
    files = sorted(f for f in out.split("\0") if f)
    if args.reverse:
        files.reverse()
    if args.limit is not None:
        files = files[:args.limit]

    if not files:
        return fail(f"no tracked files matching {spec!r}")

    # git tracks files, not directories: a directory whose every tracked file
    # is about to go would disappear from the repo (and from the working tree,
    # since git rm prunes empty parents). Find those and plant a placeholder.
    orphaned: list[str] = []
    if args.keep_dir and target.is_dir():
        tracked = [f for f in git(repo, "ls-files", "-z", "--", rel).split("\0") if f]
        doomed = set(files)
        survivors = [f for f in tracked if f not in doomed]
        for d in sorted({PurePosixPath(f).parent.as_posix() for f in files}):
            if any(s == d or s.startswith(d + "/") for s in survivors):
                continue
            if f"{d}/{args.keep_file}" in doomed:
                continue
            orphaned.append(d)

    print(f"repo      {repo}")
    print(f"branch    {branch}")
    print(f"matched   {len(files)} tracked files under {rel}/")
    print(f"message   {args.message!r}")
    print(f"commits   {len(files) + len(orphaned)} "
          f"(one per deletion{f' + {len(orphaned)} placeholder' if orphaned else ''})")
    for d in orphaned:
        print(f"keeping   {d}/ via {args.keep_file} (every tracked file in it "
              f"is being removed)")

    if args.dry_run:
        print()
        for d in orphaned:
            print(f"  would add    {d}/{args.keep_file}")
        for f in files:
            print(f"  would remove {f}")
        print("\ndry run, nothing deleted")
        return 0

    dirty = [ln for ln in git(repo, "status", "--porcelain").splitlines() if ln]
    if dirty and not args.yes:
        print(f"\nwarning: {len(dirty)} uncommitted change(s) in the working tree")
        if not confirm("continue anyway?"):
            return 1

    if not args.yes:
        if not confirm(f"\ndelete {len(files)} files in {len(files)} commits "
                       f"on '{branch}'?"):
            return 1

    done = 0
    t0 = time.time()

    for d in orphaned:
        keep = repo / d / args.keep_file
        try:
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.touch()
            git(repo, "add", "--", f"{d}/{args.keep_file}")
            git(repo, "commit", "-q", "--no-verify", "--no-gpg-sign",
                "-m", args.keep_message.format(dir=d))
            print(f"kept {d}/ via {args.keep_file}")
        except GitError as e:
            print(f"\ncould not plant {keep}: {e}", file=sys.stderr)
            return 1

    for i, f in enumerate(files, 1):
        message = args.message.format(name=Path(f).name, path=f,
                                      i=i, n=len(files))
        try:
            git(repo, "rm", "-q", "--", f)
            git(repo, "commit", "-q", "--no-verify", "--no-gpg-sign",
                "-m", message)
        except GitError as e:
            print(f"\nfailed on {f}: {e}", file=sys.stderr)
            print(f"{done} deletions committed; rerun to resume.", file=sys.stderr)
            return 1
        done += 1
        print(f"\r{done}/{len(files)}  {f[-60:]:<60}", end="", flush=True)
        if args.delay:
            time.sleep(args.delay)

    print(f"\ndone: {done} files removed in {done} commits "
          f"({time.time() - t0:.1f}s)")

    if not args.push:
        print("skipping push (--no-push)")
        return 0

    upstream = git(repo, "rev-parse", "--abbrev-ref", "-q", "@{upstream}",
                   check=False).strip()
    push_args = ["push", args.remote, branch] if upstream else \
                ["push", "-u", args.remote, branch]
    print(f"pushing {done} commits to {args.remote}/{branch} ...")
    try:
        print(git(repo, *push_args).strip() or "pushed.")
    except GitError as e:
        print(f"push failed: {e}", file=sys.stderr)
        print("deletions are committed locally; push when ready.", file=sys.stderr)
        return 1
    return 0


def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted; rerun to resume.", file=sys.stderr)
        sys.exit(130)
