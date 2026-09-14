#!/usr/bin/env bash
# Bump the plugin version in both manifests at once.
#
#   ./tools/bump-version.sh patch     0.1.0 -> 0.1.1
#   ./tools/bump-version.sh minor     0.1.0 -> 0.2.0
#   ./tools/bump-version.sh major     0.1.0 -> 1.0.0
#   ./tools/bump-version.sh 1.4.2     set explicitly
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - "${1:-patch}" <<'PY'
import json, re, sys
from collections import OrderedDict

arg = sys.argv[1]
PLUGIN, MARKET = ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"

p = json.load(open(PLUGIN), object_pairs_hook=OrderedDict)
cur = p.get("version", "0.0.0")

if re.fullmatch(r"\d+\.\d+\.\d+", arg):
    new = arg
else:
    major, minor, patch = (int(x) for x in cur.split("."))
    if arg == "major":   major, minor, patch = major + 1, 0, 0
    elif arg == "minor": minor, patch = minor + 1, 0
    elif arg == "patch": patch += 1
    else:
        sys.exit("usage: bump-version.sh [major|minor|patch|X.Y.Z]")
    new = "%d.%d.%d" % (major, minor, patch)

p["version"] = new
json.dump(p, open(PLUGIN, "w"), indent=2); open(PLUGIN, "a").write("\n")

m = json.load(open(MARKET), object_pairs_hook=OrderedDict)
m["plugins"][0]["version"] = new
json.dump(m, open(MARKET, "w"), indent=2); open(MARKET, "a").write("\n")

print("%s -> %s (both manifests)" % (cur, new))
PY
