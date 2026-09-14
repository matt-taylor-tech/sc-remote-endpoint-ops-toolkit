#!/usr/bin/env python3
"""ScreenConnect RESTful API client for the screenconnect plugin.

Calls the RESTful API Manager extension on your ScreenConnect instance.

Two ways to use it:

1. Raw method call:
     python3 sc.py <Method> [arg1] [arg2] ...
   Each arg becomes one element of the JSON body array. Args that parse as
   JSON are passed parsed; everything else as a string.

     python3 sc.py GetSessionsByName "ACE-LT067"
     python3 sc.py GetSessionsByFilter "GuestMachineSerialNumber = '7GXL8S3'"
     python3 sc.py AddNoteToSession "<sessionID>" "Reimaged and rejoined to domain"

   chat - print a session's chat transcript:
     python3 sc.py chat <sessionID|serial|machineName> [--since <iso8601>]

2. run - send a command and wait for its output (the raw API is fire-and-forget;
   output returns asynchronously as a session event, so this polls for it):
     python3 sc.py run <sessionID|serial|machineName> "<command>" [--shell powershell] [--timeout 60] [--force]
     python3 sc.py run 7GXL8S3 "systeminfo"
     python3 sc.py run ACE-LT067 "Get-EventLog -LogName System -Newest 20" --shell powershell

   --force is required for commands matching the destructive denylist.

   Resolution: a sessionID GUID targets exactly that session. A serial or
   machine name may match several sessions (reset/reprovisioned machines leave
   stale duplicates). For 'run', if more than one MATCHING session is online the
   target is ambiguous and the command is refused - pass the specific sessionID.
   Placeholder serials (e.g. "System Serial Number") are ignored; use the name.

3. setup - write a config file so the rest of the commands work:
     python3 sc.py setup --url https://<instance>.screenconnect.com --secret <secret>
   Writes ~/.config/screenconnect/screenconnect-config.json (mode 0600) and verifies
   the credentials against the instance. Use --path to write somewhere else,
   --no-verify to skip the check. With no flags it reads the values from the
   environment, which is how the SessionStart hook materializes plugin user config.

Config discovery (first match wins):
  1. SC_URL + SC_AUTH_SECRET env vars (+ optional SC_EXTENSION_ID, SC_ORIGIN)
  2. CLAUDE_PLUGIN_OPTION_SC_* (plugin user config, when the host exports it)
  3. SC_CONFIG env var (path to a screenconnect-config.json)
  4. Any mounted */mnt/Configs/screenconnect-config.json (Cowork folder mount convention)
  5. ~/.config/screenconnect/screenconnect-config.json (written by 'setup')
"""
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request


READ_METHODS = [
    "GetSessionBySessionID",
    "GetSessionDetailsBySessionID",
    "GetSessionsByFilter",
    "GetSessionsByName",
]
ACTION_METHODS = [
    "AddNoteToSession",
    "SendMessageToSession",
    "UpdateSessionName",
    "UpdateSessionCustomProperties",
    "SendCommandToSession",
    "SendToolboxItemToSession",
]
BLOCKED_METHODS = ["CreateSession"]

DESTRUCTIVE = re.compile(
    r"\bformat\s+[A-Za-z]:|\bFormat-Volume\b|\b(diskpart|mkfs|fdisk)\b"
    r"|\bdel\b.*[/\\]s|\brm\s+-rf?\s+[/~]|Remove-Item\b.*-Recurse"
    r"|\bcipher\b.*/w|vssadmin\s+delete|wbadmin\s+delete"
    r"|\bbcdedit\b|\breg\b\s+delete|Remove-Item\b.*\\Windows"
    r"|\bshutdown\b|Restart-Computer|Stop-Computer"
    r"|net\s+user\s+\S+\s+/(add|delete)|Set-ExecutionPolicy",
    re.IGNORECASE,
)

EVT_COMMAND_QUEUED = "44"
EVT_COMMAND_OUTPUT = "70"
EVT_CHAT_HOST = "45"   # technician chat message (Host = tech name)
EVT_CHAT_GUEST = "71"  # guest/end-user chat message

# Serial values that do not identify a unique machine. BIOS/OEM placeholders
# plus long all-numeric/hyphen "asset tag" defaults that many machines share.
PLACEHOLDER_SERIALS = {
    "", "system serial number", "to be filled by o.e.m.", "default string",
    "none", "0", "na", "n/a", "invalid", "not specified", "not available",
    "chassis serial number", "base board serial number", "........",
    "1234567890", "0000000000", "o.e.m.", "oem",
}


def is_identifying_serial(s):
    s = (s or "").strip()
    if s.lower() in PLACEHOLDER_SERIALS:
        return False
    if re.fullmatch(r"[0-9][0-9-]{18,}", s):  # long numeric/hyphen default tags
        return False
    return True


DEFAULT_EXTENSION_ID = "2d558935-686a-4bd0-9991-07539f5fe749"
USER_CONFIG_PATH = os.path.expanduser(
    "~/.config/screenconnect/screenconnect-config.json")

NOT_CONFIGURED = """ScreenConnect is not configured yet.

Ask the user for their ScreenConnect URL and the RESTfulAuthenticationSecret set on
the RESTful API Manager extension, then run:

  python3 {script} setup --url https://<instance>.screenconnect.com --secret <secret>

Alternatives: set SC_URL + SC_AUTH_SECRET, point SC_CONFIG at a config file, or
connect a folder containing Configs/screenconnect-config.json. If this plugin was
installed with user config filled in, restart the session so the hook can write it."""


def _env_value(name):
    """Read SC_<NAME>, falling back to the plugin user config the host exports."""
    return (os.environ.get(name)
            or os.environ.get("CLAUDE_PLUGIN_OPTION_" + name)
            or "").strip()


def _env_config():
    url, secret = _env_value("SC_URL"), _env_value("SC_AUTH_SECRET")
    if not (url and secret):
        return None
    cfg = {"url": normalize_url(url),
           "extension_id": _env_value("SC_EXTENSION_ID") or DEFAULT_EXTENSION_ID,
           "auth_secret": secret}
    origin = _env_value("SC_ORIGIN")
    if origin:
        cfg["origin"] = origin
    return cfg


def normalize_url(url):
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def find_config():
    # Inline env first (CI / serverless), then plugin user config, then files.
    cfg = _env_config()
    if cfg:
        return cfg
    candidates = []
    env = os.environ.get("SC_CONFIG")
    if env:
        candidates.append(env)
    candidates += sorted(glob.glob(
        "/sessions/*/mnt/Configs/screenconnect-config.json"))
    candidates.append(USER_CONFIG_PATH)
    for c in candidates:
        if c and os.path.isfile(c):
            with open(c) as f:
                return json.load(f)
    sys.exit("ERROR: " + NOT_CONFIGURED.format(script=sys.argv[0]))


def write_config(cfg, path):
    """Write a config file with owner-only permissions."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(path, 0o600)


def do_setup(args):
    """Write screenconnect-config.json from flags (or the environment)."""
    usage = ("usage: sc.py setup --url <https://instance.screenconnect.com> "
             "--secret <RESTfulAuthenticationSecret> [--extension-id <guid>] "
             "[--origin <origin>] [--path <file>] [--no-verify] [--quiet]")
    opts = {"--url": None, "--secret": None, "--extension-id": None,
            "--origin": None, "--path": None}
    verify, quiet = True, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--no-verify":
            verify = False; i += 1
        elif a == "--quiet":
            quiet = True; i += 1
        elif a in opts:
            if i + 1 >= len(args):
                sys.exit("ERROR: " + a + " needs a value\n" + usage)
            opts[a] = args[i + 1]; i += 2
        else:
            sys.exit("ERROR: unknown argument " + a + "\n" + usage)

    url = opts["--url"] or _env_value("SC_URL")
    secret = opts["--secret"] or _env_value("SC_AUTH_SECRET")
    if not (url and secret):
        if quiet:
            return  # hook path: nothing configured, stay silent
        sys.exit("ERROR: --url and --secret are required (or set SC_URL and "
                 "SC_AUTH_SECRET in the environment).\n" + usage)

    cfg = {"url": normalize_url(url),
           "extension_id": (opts["--extension-id"] or _env_value("SC_EXTENSION_ID")
                            or DEFAULT_EXTENSION_ID),
           "auth_secret": secret}
    origin = opts["--origin"] or _env_value("SC_ORIGIN")
    if origin:
        cfg["origin"] = origin

    path = opts["--path"] or os.environ.get("SC_CONFIG") or USER_CONFIG_PATH
    path = os.path.expanduser(path)

    if verify:
        probe = Client(cfg)
        try:
            probe.call("GetSessionsByFilter", ["Name = '__sc_setup_probe__'"])
        except SystemExit as e:
            sys.exit("ERROR: those credentials did not work, nothing was written.\n"
                     + str(e))
        except Exception as e:
            sys.exit("ERROR: could not reach " + cfg["url"] + " - " + str(e)[:200]
                     + "\nNothing was written. Check the URL, or pass --no-verify "
                     "to save anyway.")

    write_config(cfg, path)
    if not quiet:
        print("Wrote " + path + " (mode 0600)")
        print("  url:          " + cfg["url"])
        print("  extension_id: " + cfg["extension_id"])
        print("  auth_secret:  " + ("*" * 8) + " (" + str(len(secret)) + " chars)")
        if verify:
            print("Verified against the instance.")


class Client:
    def __init__(self, cfg):
        self.base = cfg["url"].rstrip("/")
        self.ext = cfg["extension_id"]
        self.headers = {
            "Content-Type": "application/json",
            "CTRLAuthHeader": cfg["auth_secret"],
            "Origin": cfg.get("origin", self.base),
        }

    def call(self, method, body):
        url = self.base + "/App_Extensions/" + self.ext + "/Service.ashx/" + method
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     method="POST", headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            sys.exit("ERROR: " + str(e.code) + " " + method + "\n" + detail)
        except urllib.error.URLError as e:
            sys.exit("ERROR: could not reach " + self.base + " - " + str(e.reason)[:200])

    def call_json(self, method, body):
        txt = self.call(method, body).strip()
        return json.loads(txt) if txt else None


GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def is_online(s):
    return len(s.get("ActiveConnections", []) or []) > 0


def last_active(s):
    gi = s.get("GuestInfo", {}) or {}
    return gi.get("LastActivityTime") or s.get("LastGuestConnectedEventTime") or ""


def rank(s):
    return (1 if is_online(s) else 0, last_active(s))


def find_sessions(client, ident):
    """Return all sessions matching ident (GUID, serial, or machine name)."""
    if GUID_RE.match(ident):
        res = client.call_json("GetSessionBySessionID", [ident])
        return res if isinstance(res, list) else ([res] if res else [])
    fields = []
    if is_identifying_serial(ident):
        fields.append("GuestMachineSerialNumber")
    fields += ["GuestMachineName", "Name"]
    for fld in fields:
        res = client.call_json("GetSessionsByFilter", [fld + " = '" + ident + "'"])
        if res:
            return res
    return []


def resolve_session(client, ident, for_command=False):
    sessions = find_sessions(client, ident)
    if not sessions:
        sys.exit("ERROR: no session found for '" + ident + "' (tried sessionID, serial, machine name).")
    sessions.sort(key=rank, reverse=True)

    def desc(s):
        return "    " + (s.get("SessionID", "?")) + "  online=" + str(is_online(s)) \
               + "  lastActive=" + (last_active(s) or "?") + "  name=" + str(s.get("Name"))

    if for_command:
        onl = [s for s in sessions if is_online(s)]
        if len(onl) == 0:
            sys.exit("ERROR: '" + ident + "' matched " + str(len(sessions))
                     + " session(s) but none are online; cannot run a command. Matches:\n"
                     + "\n".join(desc(s) for s in sessions))
        if len(onl) > 1:
            sys.exit("AMBIGUOUS: '" + ident + "' matches " + str(len(onl))
                     + " ONLINE sessions (likely reprovision duplicates). Re-run with the "
                     "specific sessionID to be sure of the target:\n"
                     + "\n".join(desc(s) for s in onl))
        chosen = onl[0]
    else:
        chosen = sessions[0]
        if len(sessions) > 1:
            sys.stderr.write("NOTE: '" + ident + "' matched " + str(len(sessions))
                             + " sessions; using the online/most-recently-active one. "
                             "Others are likely stale duplicates from reprovisioning.\n")
    return chosen["SessionID"], chosen.get("Name", ident)


def capture_command(client, sid, command, shell="powershell", timeout=25):
    """Send a command to a session and return its captured output (or None on
    timeout). Read-only convenience for playbooks that need a quick value (e.g.
    current Wi-Fi SSID). Does not log/post - caller decides."""
    if shell == "powershell":
        command = "#!ps\n" + command
    try:
        before = {e["EventID"] for e in client.call_json("GetSessionDetailsBySessionID", [sid])["Events"]}
        client.call("SendCommandToSession", [sid, command])
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            d = client.call_json("GetSessionDetailsBySessionID", [sid])
            outs = [e for e in d["Events"]
                    if e["EventID"] not in before and str(e.get("EventType")) == EVT_COMMAND_OUTPUT]
            if outs:
                return "\n".join(e.get("Data", "") for e in outs).strip()
    except Exception:
        return None
    return None


def run_command(client, ident, command, shell, timeout, force):
    if DESTRUCTIVE.search(command) and not force:
        sys.exit("REFUSED: command matches destructive pattern and --force not given:\n  " + command
                 + "\nRe-run with --force only if you are certain. Consider the ScreenConnect host page instead.")
    sid, name = resolve_session(client, ident, for_command=True)
    if shell == "powershell":
        command = "#!ps\n" + command
    # cmd is the default interpreter for plain command text; no prefix needed

    before = {e["EventID"] for e in client.call_json("GetSessionDetailsBySessionID", [sid])["Events"]}
    client.call("SendCommandToSession", [sid, command])

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        d = client.call_json("GetSessionDetailsBySessionID", [sid])
        outputs = [e for e in d["Events"]
                   if e["EventID"] not in before and str(e.get("EventType")) == EVT_COMMAND_OUTPUT]
        if outputs:
            joined = "\n".join(e.get("Data", "") for e in outputs)
            print("# session " + name + " (" + sid + ")")
            print(joined)
            return
    sys.exit("TIMEOUT: no command output after " + str(timeout)
             + "s. Machine may be offline or the command is still running. "
             "Check the session in ScreenConnect.")


def get_chat(client, sid, since=None):
    """Chat messages (tech EventType 45 / guest 71) for a session, oldest first.
    If `since` (ISO8601) is given, only messages at/after it are returned - compared on
    the first 19 chars (YYYY-MM-DDTHH:MM:SS) to sidestep fractional-second/zone mismatch."""
    d = client.call_json("GetSessionDetailsBySessionID", [sid])
    cutoff = str(since)[:19] if since else None
    msgs = []
    for e in d.get("Events", []):
        t = str(e.get("EventType"))
        tm = e.get("Time", "") or ""
        if cutoff and str(tm)[:19] < cutoff:
            continue
        if t == EVT_CHAT_HOST:
            msgs.append((tm, (e.get("Host") or "Tech"), e.get("Data") or ""))
        elif t == EVT_CHAT_GUEST:
            msgs.append((tm, "Guest", e.get("Data") or ""))
    msgs.sort(key=lambda m: m[0])
    return msgs


# Stable marker in the chat-capture note header, so a resolve-then-close (or webhook retry)
# can detect an already-posted transcript and not double-post (issue #47).


def chat_command(client, ident, since=None):
    sid, name = resolve_session(client, ident, for_command=False)
    msgs = get_chat(client, sid, since=since)
    if not msgs:
        print("# no chat messages on session " + name + " (" + sid + ")"
              + (" since " + str(since) if since else ""))
        return
    lines = [f"[{t[:16].replace('T', ' ')}] {who}: {txt}" for t, who, txt in msgs]
    transcript = "\n".join(lines)
    print("# chat transcript - " + name + " (" + sid + ")")
    print(transcript)


def raw_call(client, method, args):
    if method in BLOCKED_METHODS:
        sys.exit("ERROR: " + method + " is blocked by policy in this plugin.")
    if method not in READ_METHODS + ACTION_METHODS:
        sys.exit("ERROR: unknown method " + method + ". Allowed: "
                 + ", ".join(READ_METHODS + ACTION_METHODS) + ", or 'run'.")
    body = []
    for raw in args:
        try:
            body.append(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            body.append(raw)
    txt = client.call(method, body).strip()
    if not txt:
        print("OK (no content) - " + method + " succeeded")
        return
    try:
        print(json.dumps(json.loads(txt), indent=1, default=str))
    except (json.JSONDecodeError, ValueError):
        print(txt)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1] == "setup":
        do_setup(sys.argv[2:])
        return
    client = Client(find_config())

    if sys.argv[1] == "chat":
        rest = sys.argv[2:]
        pos = []
        i = 0
        since = None
        while i < len(rest):
            if rest[i] == "--since":
                since = rest[i + 1]; i += 2
            else:
                pos.append(rest[i]); i += 1
        if not pos:
            sys.exit("usage: sc.py chat <sessionID|serial|machineName> [--since <iso8601>]")
        chat_command(client, pos[0], since=since)
        return
    if sys.argv[1] == "run":
        rest = sys.argv[2:]
        shell, timeout, force = "cmd", 60, False
        pos = []
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--shell":
                shell = rest[i + 1]; i += 2
            elif a == "--timeout":
                timeout = int(rest[i + 1]); i += 2
            elif a == "--force":
                force = True; i += 1
            else:
                pos.append(a); i += 1
        if len(pos) < 2:
            sys.exit("usage: sc.py run <sessionID|serial|machineName> \"<command>\" "
                     "[--shell powershell|cmd] [--timeout 60] [--force]")
        run_command(client, pos[0], pos[1], shell, timeout, force)
        return

    raw_call(client, sys.argv[1], sys.argv[2:])


if __name__ == "__main__":
    main()
