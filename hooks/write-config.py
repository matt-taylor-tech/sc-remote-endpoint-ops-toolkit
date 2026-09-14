#!/usr/bin/env python3
"""Materialize plugin user config into a config file the sessions skill can read.

Runs as a SessionStart hook. The host exports the plugin's userConfig values as
CLAUDE_PLUGIN_OPTION_* environment variables to hook processes, but not to the
Bash calls the skill makes, so this bridges the two: it writes
~/.config/screenconnect/screenconnect-config.json (mode 0600) from those values.

Stdlib only, and it never fails the session: if nothing is configured, or the
write fails, it exits 0 quietly and the skill falls back to its other config
sources (env vars, SC_CONFIG, a mounted Configs/ folder).
"""
import json
import os
import sys

DEFAULT_EXTENSION_ID = "2d558935-686a-4bd0-9991-07539f5fe749"
CONFIG_PATH = os.path.expanduser(
    "~/.config/screenconnect/screenconnect-config.json")


def opt(name):
    return (os.environ.get("CLAUDE_PLUGIN_OPTION_" + name)
            or os.environ.get(name) or "").strip()


def main():
    url, secret = opt("SC_URL"), opt("SC_AUTH_SECRET")
    if not (url and secret):
        return 0
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    cfg = {"url": url.rstrip("/"),
           "extension_id": opt("SC_EXTENSION_ID") or DEFAULT_EXTENSION_ID,
           "auth_secret": secret}
    origin = opt("SC_ORIGIN")
    if origin:
        cfg["origin"] = origin
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.chmod(CONFIG_PATH, 0o600)
    except OSError as e:
        sys.stderr.write("screenconnect: could not write config: %s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
