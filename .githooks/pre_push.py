#!/usr/bin/env python3
"""Refuse a push that changes the plugin without bumping its version.

Cowork and Claude Code detect plugin updates by comparing the version in the
marketplace entry, so shipping a change without bumping it means installed
copies may never pick it up.

Install once in a clone:  git config core.hooksPath .githooks
Bypass a single push:     git push --no-verify
"""
import json
import subprocess
import sys

ZERO = "0" * 40
WATCHED = (".claude-plugin/", "skills/", "hooks/", "tools/")
PLUGIN = ".claude-plugin/plugin.json"
MARKET = ".claude-plugin/marketplace.json"


def git(*args):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def version_at(ref, path, marketplace=False):
    blob = git("show", "%s:%s" % (ref, path))
    if blob is None:
        return None
    try:
        d = json.loads(blob)
        return d["plugins"][0].get("version") if marketplace else d.get("version")
    except (ValueError, KeyError, IndexError):
        return None


def main():
    problems = []
    for line in sys.stdin:
        parts = line.split()
        if len(parts) < 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if local_sha == ZERO:
            continue  # branch deletion
        if remote_sha == ZERO:
            continue  # brand new branch, nothing to compare against

        changed = git("diff", "--name-only", "%s..%s" % (remote_sha, local_sha))
        if not changed:
            continue
        touched = [f for f in changed.split("\n")
                   if f and f.startswith(WATCHED)]
        if not touched:
            continue

        old = version_at(remote_sha, PLUGIN)
        new = version_at(local_sha, PLUGIN)
        if old and new and old == new:
            problems.append(
                "  %d plugin file(s) changed but version is still %s\n"
                "    e.g. %s" % (len(touched), new, ", ".join(touched[:3])))

        pv = version_at(local_sha, PLUGIN)
        mv = version_at(local_sha, MARKET, marketplace=True)
        if pv and mv and pv != mv:
            problems.append(
                "  version mismatch: plugin.json says %s, marketplace entry says %s"
                % (pv, mv))

    if problems:
        sys.stderr.write(
            "\nPush blocked: version not bumped.\n\n"
            + "\n".join(problems)
            + "\n\nFix:    ./tools/bump-version.sh patch   (or minor / major / 1.2.3)\n"
              "        then amend or add a commit and push again.\n"
              "Skip:   git push --no-verify\n\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
