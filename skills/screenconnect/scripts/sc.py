#!/usr/bin/env python3
"""ScreenConnect RESTful API client for the screenconnect plugin.

Calls the RESTful API Manager extension on your ScreenConnect instance. Standard
library only. On Windows, run it with `python` (or `py -3`) instead of `python3`.

Raw method call:
    sc.py <Method> [arg1] [arg2] ...
  Each arg becomes one element of the JSON body array. Args that parse as JSON are
  passed parsed; everything else as a string.
    sc.py GetSessionsByName "DESKTOP-ABC123"
    sc.py GetSessionsByFilter "GuestMachineSerialNumber = '7GXL8S3'"
    sc.py AddNoteToSession "<sessionID>" "Reimaged and rejoined to domain"

Commands:
  run <target> "<command>" | --file <path> | -     send a command, wait for output
      [--shell cmd|powershell|sh] [--timeout 60] [--max-output 100000]
      [--allow <category,...>] [--force] [--no-exit-code] [--detach [--job-hours 4]]
  run-many <t1,t2,...|@listfile> "<command>" | --file <path> | -
      [same flags as run, except --detach] [--pace 2]
  online [<target> ...] [--json]                  who is connected (all, or a list)
  push <target> <localfile> <remotepath> [--overwrite] [--chunk-kb 16] [--max-mb 5]
  job <target> <jobID> [--tail 40] [--wait <seconds>] [--cleanup [--force]]
  job <target> --list
  chat <target> [--since <iso8601>]               chat transcript
  setup --url <url> --secret <secret> [...]       write the config file

  <target> is a sessionID GUID, a serial number, or a machine name.

run details:
  --file reads the command from a file ('-' reads stdin, as does a bare '-' in
  place of the command), so it never passes through a shell's quoting. A .ps1
  file implies --shell powershell.
  --timeout also sets the agent's own kill timer (#timeout=), and output is allowed
  up to --max-output characters (#maxlength=). Without these the agent kills a
  command at 10 s and truncates output at 5000 characters.
  The remote exit code becomes sc.py's exit code (see Exit codes). --no-exit-code
  sends the command without the extra exit-code line.
  --detach runs the command as a one-off SYSTEM scheduled task on a Windows
  endpoint, logging to a file there; read it with `job`.
  The shell defaults to cmd on Windows endpoints and sh elsewhere (from the
  session's reported OS).

Denylist: run refuses commands that match a denylist category unless the operator
has confirmed and you pass --allow <category> (just those checks) or --force (all).
Categories: disk, delete, backups, boot, registry, power, accounts, execpolicy,
defender, firewall, rdp, services, logs.

Resolution: a sessionID GUID targets exactly that session. A serial or machine name
may match several sessions (reset/reprovisioned machines leave stale duplicates).
For commands, if more than one matching session is online the target is ambiguous
and the command is refused: pass the specific sessionID. Placeholder serials (e.g.
"System Serial Number") are ignored; use the name.

Exit codes:
  0-123  the remote command's exit code (0 when it can't be determined)
  2      usage error                   124  timed out, or killed by the agent
  125    target not found, offline, or ambiguous
  126    refused by the denylist       255  config, network or API error
  run-many and online exit 0 when every target succeeded, 1 otherwise.

Config discovery (first match wins):
  1. SC_URL + SC_AUTH_SECRET env vars (+ optional SC_EXTENSION_ID, SC_ORIGIN)
  2. CLAUDE_PLUGIN_OPTION_SC_* (plugin user config, when the host exports it)
  3. SC_CONFIG env var (path to a screenconnect-config.json)
  4. Any mounted */mnt/Configs/screenconnect-config.json (Cowork folder mount convention)
  5. ~/.config/screenconnect/screenconnect-config.json (written by 'setup')

Audit log: every command-sending action appends one JSON line (time, target,
SHA-256 and length of the command, result) to ~/.config/screenconnect/audit.log,
never the command text itself. SC_AUDIT_LOG=<path> moves it; SC_AUDIT_LOG=off
turns it off.
"""
import base64
import glob
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


EXIT_USAGE = 2
EXIT_TIMEOUT = 124
EXIT_TARGET = 125
EXIT_REFUSED = 126
EXIT_ERROR = 255

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

# Each rule: (category, what it guards, pattern). Matching is on the command text,
# case-insensitive, one line at a time where it matters ([^\r\n]*).
DENY_RULES = [
    ("disk", "disk formatting or partitioning",
     r"\bformat(\.com)?\s+[A-Za-z]:|\bFormat-Volume\b|\bClear-Disk\b|\bInitialize-Disk\b"
     r"|\b(diskpart|mkfs|fdisk)\b"),
    ("delete", "recursive or system-folder deletion",
     r"\bdel\b[^\r\n]*[/\\]s\b|\b(rd|rmdir)\b[^\r\n]*/s\b|\brm\s+-[a-z]*r[a-z]*\s+[/~]"
     r"|\bRemove-Item\b[^\r\n]*-Recurse|\bRemove-Item\b[^\r\n]*\\Windows\b|\bcipher\b[^\r\n]*/w"),
    ("backups", "deleting shadow copies or backups",
     r"\bvssadmin(\.exe)?\s+delete|\bwbadmin(\.exe)?\s+delete|\bshadowcopy\s+delete"),
    ("boot", "boot configuration changes", r"\bbcdedit\b"),
    ("registry", "registry key deletion", r"\breg(\.exe)?\s+delete\b"),
    ("power", "shutdown or restart",
     r"\bshutdown(\.exe)?\b|\bRestart-Computer\b|\bStop-Computer\b"),
    ("accounts", "creating, deleting or elevating accounts",
     r"\bnet1?\s+user\s+\S+[^\r\n]*/(add|delete)\b"
     r"|\bnet1?\s+localgroup\s+administrators\b[^\r\n]*/add\b"
     r"|\b(New|Remove)-LocalUser\b|\bAdd-LocalGroupMember\b[^\r\n]*Administrators"),
    ("execpolicy", "changing the PowerShell execution policy", r"\bSet-ExecutionPolicy\b"),
    ("defender", "weakening Microsoft Defender",
     r"\bSet-MpPreference\b[^\r\n]*-Disable\w+\s+(\$true|1)\b"
     r"|\b(Add|Set)-MpPreference\b[^\r\n]*-Exclusion\w*"
     r"|\b(sc(\.exe)?|net)\s+stop\s+(WinDefend|Sense)\b"
     r"|\bStop-Service\b[^\r\n]*\b(WinDefend|Sense)\b"
     r"|\b(Uninstall|Remove)-WindowsFeature\b[^\r\n]*Defender"
     r"|\bDisableAntiSpyware\b[^\r\n]*\b1\b"),
    ("firewall", "disabling the firewall or its rules",
     r"\bDisable-NetFirewallRule\b|\bRemove-NetFirewallRule\b"
     r"|\bSet-NetFirewallProfile\b[^\r\n]*-Enabled\s+(\$false|False|0)\b"
     r"|\bnetsh\s+advfirewall\s+set\s+\S+\s+state\s+off\b"
     r"|\bnetsh\s+firewall\s+set\s+opmode\s+(mode=)?disable"),
    ("rdp", "enabling Remote Desktop",
     r"\bfDenyTSConnections\b[^\r\n]*?(-Value\s+|/d\s+|=\s*)0\b"
     r"|\bEnable-NetFirewallRule\b[^\r\n]*Remote\s*Desktop"),
    ("services", "deleting services",
     r"\bsc(\.exe)?\s+(\\\\\S+\s+)?delete\b|\bRemove-Service\b"),
    ("logs", "clearing event or audit logs",
     r"\bwevtutil(\.exe)?\s+(cl|clear-log)\b|\bClear-EventLog\b|\bRemove-EventLog\b"
     r"|\bauditpol(\.exe)?\b[^\r\n]*/(clear|remove)\b"),
]
DENY = [(c, d, re.compile(p, re.IGNORECASE)) for c, d, p in DENY_RULES]
DENY_CATEGORIES = [c for c, _, _ in DENY_RULES]

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


def die(msg, code=EXIT_ERROR):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(code)


def note(msg):
    sys.stdout.flush()  # keep notes after the output they describe
    sys.stderr.write(msg.rstrip("\n") + "\n")


def utf8_stdio():
    """Event logs and command output often contain characters (e.g. U+200E) that a
    legacy Windows console code page can't encode; don't crash on them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def float_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def is_identifying_serial(s):
    s = (s or "").strip()
    if s.lower() in PLACEHOLDER_SERIALS:
        return False
    if re.fullmatch(r"[0-9][0-9-]{18,}", s):  # long numeric/hyphen default tags
        return False
    return True


# --------------------------------------------------------------------- arguments

def parse_flags(args, spec, usage):
    """spec maps '--flag' to 'bool', 'str', 'int' or 'float'. Returns (opts, positionals).
    A lone '-' is a positional (stdin)."""
    opts = {k.lstrip("-").replace("-", "_"): (False if t == "bool" else None)
            for k, t in spec.items()}
    pos = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in spec:
            kind = spec[a]
            key = a.lstrip("-").replace("-", "_")
            if kind == "bool":
                opts[key] = True
                i += 1
                continue
            if i + 1 >= len(args):
                die("ERROR: " + a + " needs a value\n" + usage, EXIT_USAGE)
            raw = args[i + 1]
            try:
                opts[key] = int(raw) if kind == "int" else float(raw) if kind == "float" else raw
            except ValueError:
                die("ERROR: " + a + " needs a number, got '" + raw + "'\n" + usage, EXIT_USAGE)
            i += 2
        elif a.startswith("--"):
            die("ERROR: unknown option " + a + "\n" + usage, EXIT_USAGE)
        else:
            pos.append(a)
            i += 1
    return opts, pos


def parse_allow(value):
    if not value:
        return set()
    cats = {c.strip().lower() for c in value.split(",") if c.strip()}
    unknown = cats - set(DENY_CATEGORIES)
    if unknown:
        die("ERROR: unknown --allow categor" + ("ies " if len(unknown) > 1 else "y ")
            + ", ".join(sorted(unknown)) + ". Known: " + ", ".join(DENY_CATEGORIES), EXIT_USAGE)
    return cats


def read_text(path):
    try:
        if path == "-":
            data = sys.stdin.buffer.read()
        else:
            with open(path, "rb") as f:
                data = f.read()
    except OSError as e:
        die("ERROR: can't read " + path + ": " + str(e), EXIT_USAGE)
    return data.decode("utf-8-sig")  # drops a BOM an editor may have added


def command_source(pos_command, file_opt, usage):
    """Return (command, implied_shell). A file keeps every backslash and quote as
    written, which a command-line argument passed through a shell may not."""
    if file_opt and pos_command is not None:
        die("ERROR: give the command either inline or with --file, not both\n" + usage, EXIT_USAGE)
    if file_opt:
        ext = os.path.splitext(file_opt)[1].lower()
        implied = "powershell" if ext == ".ps1" else "cmd" if ext in (".cmd", ".bat") else None
        return read_text(file_opt), implied
    if pos_command is None:
        die("ERROR: no command given\n" + usage, EXIT_USAGE)
    if pos_command == "-":
        return read_text("-"), None
    return pos_command, None


# --------------------------------------------------------------------- config

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
    die("ERROR: " + NOT_CONFIGURED.format(script=sys.argv[0]))


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
    opts, pos = parse_flags(args, {"--url": "str", "--secret": "str", "--extension-id": "str",
                                   "--origin": "str", "--path": "str",
                                   "--no-verify": "bool", "--quiet": "bool"}, usage)
    if pos:
        die("ERROR: unexpected argument " + pos[0] + "\n" + usage, EXIT_USAGE)
    verify, quiet = not opts["no_verify"], opts["quiet"]

    url = opts["url"] or _env_value("SC_URL")
    secret = opts["secret"] or _env_value("SC_AUTH_SECRET")
    if not (url and secret):
        if quiet:
            return  # hook path: nothing configured, stay silent
        die("ERROR: --url and --secret are required (or set SC_URL and "
            "SC_AUTH_SECRET in the environment).\n" + usage, EXIT_USAGE)

    cfg = {"url": normalize_url(url),
           "extension_id": (opts["extension_id"] or _env_value("SC_EXTENSION_ID")
                            or DEFAULT_EXTENSION_ID),
           "auth_secret": secret}
    origin = opts["origin"] or _env_value("SC_ORIGIN")
    if origin:
        cfg["origin"] = origin

    path = opts["path"] or os.environ.get("SC_CONFIG") or USER_CONFIG_PATH
    path = os.path.expanduser(path)

    if verify:
        try:
            Client(cfg).request("GetSessionsByFilter", ["Name = '__sc_setup_probe__'"])
        except SCError as e:
            if e.kind == "network":
                die("ERROR: nothing reached the instance, so the secret was never "
                    "tested. Nothing was written.\n  " + str(e) + "\n\n"
                    "A 403 at CONNECT or 'Tunnel connection failed' means a sandbox "
                    "network\nallowlist blocked the host before the request left - "
                    "not a bad secret.\nSee 'Network access' in the plugin README. "
                    "Pass --no-verify to save anyway.")
            die("ERROR: the instance rejected the request, nothing was written.\n  "
                + str(e) + "\n\nCheck the RESTfulAuthenticationSecret and that the "
                "RESTful API Manager extension\nis installed. Pass --no-verify to "
                "save anyway.")

    write_config(cfg, path)
    if not quiet:
        print("Wrote " + path + " (mode 0600)")
        print("  url:          " + cfg["url"])
        print("  extension_id: " + cfg["extension_id"])
        print("  auth_secret:  " + ("*" * 8) + " (" + str(len(secret)) + " chars)")
        if verify:
            print("Verified against the instance.")


# --------------------------------------------------------------------- HTTP client

class SCError(Exception):
    """kind is 'http' (the instance answered with an error) or 'network'
    (nothing reached the instance)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


# 429 and 503 mean the request was turned away, so any method may be retried.
# 502/504 and timeouts may arrive after the instance acted, so only reads are
# retried on those: a retried SendCommandToSession could run a command twice.
RETRY_ANY = {429, 503}
RETRY_READ = {502, 504}


class Client:
    def __init__(self, cfg, retries=3):
        self.base = cfg["url"].rstrip("/")
        self.ext = cfg["extension_id"]
        self.headers = {
            "Content-Type": "application/json",
            "CTRLAuthHeader": cfg["auth_secret"],
            "Origin": cfg.get("origin", self.base),
        }
        self.retries = retries
        self.retry_base = float_env("SC_RETRY_BASE", 1.0)

    def _delay(self, attempt, retry_after=None):
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return self.retry_base * (2 ** (attempt - 1))

    def request(self, method, body):
        """Raise SCError on failure. Use call() for the exit-on-error behavior."""
        url = self.base + "/App_Extensions/" + self.ext + "/Service.ashx/" + method
        data = json.dumps(body).encode()
        read = method in READ_METHODS
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method="POST", headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                retryable = e.code in RETRY_ANY or (read and e.code in RETRY_READ)
                if retryable and attempt < self.retries:
                    attempt += 1
                    time.sleep(self._delay(attempt, e.headers.get("Retry-After")))
                    continue
                raise SCError("http", str(e.code) + " " + method + "\n" + detail)
            except urllib.error.URLError as e:
                transient = isinstance(e.reason, (socket.timeout, TimeoutError, ConnectionResetError))
                if read and transient and attempt < self.retries:
                    attempt += 1
                    time.sleep(self._delay(attempt))
                    continue
                raise SCError("network",
                              "could not reach " + self.base + " - " + str(e.reason)[:200])
            except (socket.timeout, TimeoutError):
                if read and attempt < self.retries:
                    attempt += 1
                    time.sleep(self._delay(attempt))
                    continue
                raise SCError("network", "timed out waiting for " + self.base + " (" + method + ")")

    def call(self, method, body):
        try:
            return self.request(method, body)
        except SCError as e:
            die("ERROR: " + str(e))

    def call_json(self, method, body):
        txt = self.call(method, body).strip()
        return json.loads(txt) if txt else None


# --------------------------------------------------------------------- audit log

def audit_path():
    v = os.environ.get("SC_AUDIT_LOG", "").strip()
    if v.lower() in ("off", "0", "false", "no", "none"):
        return None
    return os.path.expanduser(v) if v else os.path.join(os.path.dirname(USER_CONFIG_PATH), "audit.log")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_audit_warned = False


def audit(action, **fields):
    """One JSON line per command-sending action. Hashes only: command text and
    output can hold secrets, so they never go in the log."""
    global _audit_warned
    path = audit_path()
    if not path:
        return
    rec = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "action": action}
    rec.update({k: v for k, v in fields.items() if v is not None})
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as e:
        if not _audit_warned:
            note("NOTE: couldn't write the audit log (" + str(e) + "). Set SC_AUDIT_LOG to "
                 "another path, or to 'off'.")
            _audit_warned = True


# --------------------------------------------------------------------- sessions

GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
LOOKUP_ATTEMPTS = 3


class TargetError(Exception):
    pass


def q(value):
    """Quote a value for a session filter expression."""
    return "'" + str(value).replace("'", "''") + "'"


def is_online(s):
    return len(s.get("ActiveConnections", []) or []) > 0


def _when(value):
    """ScreenConnect reports 'never' as 0001-01-01; treat it as empty."""
    return "" if not value or str(value).startswith("0001-") else value


def last_active(s):
    gi = s.get("GuestInfo", {}) or {}
    return _when(gi.get("LastActivityTime")) or _when(s.get("LastGuestConnectedEventTime")) or ""


def is_windows(s):
    os_name = ((s.get("GuestInfo") or {}).get("OperatingSystemName") or "")
    return (not os_name) or ("windows" in os_name.lower())  # unknown: assume Windows


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
        res = client.call_json("GetSessionsByFilter", [fld + " = " + q(ident)])
        if res:
            return res
    return []


def _desc(s):
    return ("    " + (s.get("SessionID", "?")) + "  online=" + str(is_online(s))
            + "  lastActive=" + (last_active(s) or "?") + "  name=" + str(s.get("Name")))


def pick_session(client, ident, for_command=False):
    """Return (session, note-or-None). Raise TargetError when there's no usable match.
    An empty answer is retried: an instance under load can answer a lookup for a
    session that exists with an empty result."""
    sessions = []
    for attempt in range(LOOKUP_ATTEMPTS):
        sessions = find_sessions(client, ident)
        if sessions:
            break
        if attempt < LOOKUP_ATTEMPTS - 1:
            time.sleep(client.retry_base * 1.5 * (attempt + 1))
    if not sessions:
        raise TargetError(
            "no session found for '" + ident + "' (tried sessionID, serial and machine name, "
            + str(LOOKUP_ATTEMPTS) + " attempts). If the machine does exist and you're calling "
            "in a tight loop, the instance may be answering with empty results: slow down, "
            "or use run-many with --pace.")
    sessions.sort(key=rank, reverse=True)
    if for_command:
        onl = [s for s in sessions if is_online(s)]
        if not onl:
            raise TargetError(
                "'" + ident + "' matched " + str(len(sessions))
                + " session(s) but none are online; cannot run a command. Matches:\n"
                + "\n".join(_desc(s) for s in sessions))
        if len(onl) > 1:
            raise TargetError(
                "AMBIGUOUS: '" + ident + "' matches " + str(len(onl))
                + " ONLINE sessions (likely reprovision duplicates). Re-run with the "
                "specific sessionID to be sure of the target:\n"
                + "\n".join(_desc(s) for s in onl))
        return onl[0], None
    msg = None
    if len(sessions) > 1:
        msg = ("NOTE: '" + ident + "' matched " + str(len(sessions))
               + " sessions; using the online/most-recently-active one. "
               "Others are likely stale duplicates from reprovisioning.")
    return sessions[0], msg


def resolve(client, ident, for_command=False):
    """Like pick_session, but exits on failure. Returns the session dict."""
    try:
        s, msg = pick_session(client, ident, for_command)
    except TargetError as e:
        die("ERROR: " + str(e), EXIT_TARGET)
    if msg:
        note(msg)
    return s


def resolve_session(client, ident, for_command=False):
    s = resolve(client, ident, for_command)
    return s["SessionID"], s.get("Name", ident)


def session_online(client, sid):
    """True/False, or None if the lookup itself failed."""
    try:
        txt = client.request("GetSessionBySessionID", [sid]).strip()
        res = json.loads(txt) if txt else None
    except (SCError, ValueError):
        return None
    s = res[0] if isinstance(res, list) and res else res
    return is_online(s) if isinstance(s, dict) else False


# --------------------------------------------------------------------- denylist

def denied(command, allow=()):
    hits = []
    for cat, desc, rx in DENY:
        if cat in allow:
            continue
        m = rx.search(command)
        if m:
            hits.append((cat, desc, m.group(0)))
    return hits


def refuse_if_denied(command, allow=(), force=False):
    if force:
        return
    hits = denied(command, allow)
    if not hits:
        return
    lines = ["REFUSED: the command matches the denylist:"]
    for cat, desc, snip in hits:
        lines.append("  [" + cat + "] " + desc + ": " + " ".join(snip.split())[:120])
    cats = ",".join(sorted({h[0] for h in hits}))
    lines.append("Confirm the exact command with the operator, then re-run with --allow " + cats
                 + " (skips only those checks), or --force (skips every check).")
    lines.append("Matching is on the command text, so a pattern inside a string, or a script "
                 "being written to disk, counts too.")
    die("\n".join(lines), EXIT_REFUSED)


# --------------------------------------------------------------------- run

# The agent kills a command after 10 s and cuts its output at 5000 characters
# unless the command opens with #timeout=<ms> / #maxlength=<chars> lines, so every
# command carries both, with the agent timeout matched to how long we poll.
MAX_OUTPUT_CHARS = 100000
EXIT_MARK = "__SC_EXIT__="
EXIT_RE = re.compile(r"^\s*__SC_EXIT__=(-?\d+)\s*$", re.M)
KILLED_RE = re.compile(r"Killed after (\d+) milliseconds")
TRUNC_RE = re.compile(r"Truncated output at (\d+) characters")


def default_shell(session, requested):
    if requested:
        return requested
    return "cmd" if is_windows(session) else "sh"


def wrap_command(command, shell, timeout, max_output=MAX_OUTPUT_CHARS, exit_code=True):
    head = "#!ps\n" if shell == "powershell" else ""  # cmd/sh: the agent's default interpreter
    head += "#timeout=" + str(int(timeout) * 1000) + "\n#maxlength=" + str(int(max_output)) + "\n"
    if not exit_code:
        return head + command
    body = command.rstrip("\r\n")
    if shell == "powershell":
        # $? must be read by the very next statement after the operator's last line.
        return (head + body + "\n$__sc_ok = $?\n"
                + '"' + EXIT_MARK + '$(if ($__sc_ok) { 0 } elseif ($LASTEXITCODE) '
                + '{ $LASTEXITCODE } else { 1 })"\n')
    if shell == "cmd":
        return head + body + "\r\n@echo " + EXIT_MARK + "%ERRORLEVEL%\r\n"
    return head + body + '\necho "' + EXIT_MARK + '$?"\n'


def split_exit(text):
    """Remove the exit-code line; return (clean_text, code or None)."""
    matches = list(EXIT_RE.finditer(text))
    if not matches:
        return text, None
    m = matches[-1]
    return (text[:m.start()] + text[m.end():]).rstrip(), int(m.group(1))


def _events(client, sid):
    d = client.call_json("GetSessionDetailsBySessionID", [sid]) or {}
    return d.get("Events", []) or []


def execute(client, sid, wrapped, timeout):
    """Send an already-wrapped command and wait for its output event.
    Returns a dict: status ok|killed|timeout|offline, output, exit, killed_ms, truncated."""
    before = {e.get("EventID") for e in _events(client, sid)}
    client.call("SendCommandToSession", [sid, wrapped])
    # The agent's own timer equals `timeout`; allow a little longer for its
    # "Killed after" report to arrive.
    deadline = time.time() + timeout + (3 if timeout >= 10 else 1)
    interval = float_env("SC_POLL_INTERVAL", 1.5)
    while time.time() < deadline:
        time.sleep(max(0.0, min(interval, deadline - time.time())))
        interval = min(interval * 1.5, 5.0)
        outs = [e for e in _events(client, sid)
                if e.get("EventID") not in before and str(e.get("EventType")) == EVT_COMMAND_OUTPUT]
        if outs:
            raw = "\n".join(e.get("Data") or "" for e in outs)
            out, code = split_exit(raw)
            km, tm = KILLED_RE.search(out), TRUNC_RE.search(out)
            return {"status": "killed" if km else "ok", "output": out, "exit": code,
                    "killed_ms": int(km.group(1)) if km else None,
                    "truncated": int(tm.group(1)) if tm else None}
    online = session_online(client, sid)
    return {"status": "offline" if online is False else "timeout", "output": "", "exit": None,
            "killed_ms": None, "truncated": None}


def explain(res, name, timeout, exit_expected):
    """Print stderr notes for a result and return the exit code to use."""
    st = res["status"]
    if res.get("truncated"):
        note("NOTE: the agent truncated the output at " + str(res["truncated"])
             + " characters. Raise --max-output, or have the command write to a file on the endpoint.")
    if st == "killed":
        note("KILLED: the agent stopped the command after " + str(res["killed_ms"])
             + " ms. Raise --timeout, or use --detach for long jobs.")
        return EXIT_TIMEOUT
    if st == "timeout":
        note("TIMEOUT: no output after " + str(timeout) + "s. " + name + " is still online, so the "
             "command is probably still running. Raise --timeout, or use --detach for long jobs.")
        return EXIT_TIMEOUT
    if st == "offline":
        note("OFFLINE: " + name + " disconnected while waiting for output. The command may or may "
             "not have run; check once the machine is back.")
        return EXIT_TARGET
    code = res.get("exit")
    if code is None:
        if exit_expected:
            note("NOTE: exit code not reported (the command ended with 'exit', or its output was "
                 "cut short).")
        return 0
    if code != 0:
        note("# remote exit code: " + str(code))
    return code if 0 <= code <= 123 else 1


RUN_SPEC = {"--shell": "str", "--timeout": "int", "--max-output": "int", "--file": "str",
            "--allow": "str", "--force": "bool", "--no-exit-code": "bool",
            "--detach": "bool", "--job-hours": "int"}
RUN_USAGE = ('usage: sc.py run <sessionID|serial|machineName> "<command>" | --file <path> | - '
             "[--shell cmd|powershell|sh] [--timeout 60] [--max-output 100000] "
             "[--allow <category,...>] [--force] [--no-exit-code] [--detach [--job-hours 4]]")


def run_opts(opts, usage):
    shell = opts["shell"]
    if shell and shell not in ("cmd", "powershell", "sh"):
        die("ERROR: --shell must be cmd, powershell or sh\n" + usage, EXIT_USAGE)
    timeout = opts["timeout"] if opts["timeout"] is not None else 60
    if timeout < 1:
        die("ERROR: --timeout must be at least 1\n" + usage, EXIT_USAGE)
    max_output = opts["max_output"] if opts["max_output"] is not None else MAX_OUTPUT_CHARS
    if max_output < 1:
        die("ERROR: --max-output must be at least 1\n" + usage, EXIT_USAGE)
    return shell, timeout, max_output


def run_command(client, ident, command, shell=None, timeout=60, force=False, allow=(),
                max_output=MAX_OUTPUT_CHARS, exit_code=True):
    refuse_if_denied(command, allow, force)
    s = resolve(client, ident, for_command=True)
    sid, name = s["SessionID"], s.get("Name", ident)
    sh = default_shell(s, shell)
    res = execute(client, sid, wrap_command(command, sh, timeout, max_output, exit_code), timeout)
    audit("run", target=ident, session=sid, name=name, shell=sh, sha256=digest(command),
          chars=len(command), allow=",".join(sorted(allow)) or None, force=force or None,
          status=res["status"], exit=res["exit"])
    print("# session " + name + " (" + sid + ")")
    if res["output"]:
        print(res["output"])
    return explain(res, name, timeout, exit_code)


def capture_command(client, sid, command, shell="powershell", timeout=25):
    """Send a read-only command to a session and return its output, or None if it
    didn't finish in time. API errors exit, as everywhere else in sc.py."""
    res = execute(client, sid, wrap_command(command, shell, timeout, exit_code=False), timeout)
    return res["output"].strip() if res["status"] == "ok" else None


def cmd_run(client, args):
    opts, pos = parse_flags(args, RUN_SPEC, RUN_USAGE)
    if not pos or len(pos) > 2:
        die(RUN_USAGE, EXIT_USAGE)
    command, implied = command_source(pos[1] if len(pos) > 1 else None, opts["file"], RUN_USAGE)
    shell, timeout, max_output = run_opts(opts, RUN_USAGE)
    shell = shell or implied
    allow = parse_allow(opts["allow"])
    if opts["detach"]:
        return detach(client, pos[0], command, shell, allow, opts["force"], opts["job_hours"] or 4)
    return run_command(client, pos[0], command, shell, timeout, opts["force"], allow,
                       max_output, not opts["no_exit_code"])


# --------------------------------------------------------------------- run-many

def parse_targets(spec):
    if spec.startswith("@"):
        text = read_text(spec[1:])
        items = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
    else:
        items = re.split(r"[,\s]+", spec)
    seen, out = set(), []
    for t in items:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


RUN_MANY_USAGE = ('usage: sc.py run-many <t1,t2,...|@listfile> "<command>" | --file <path> | - '
                  "[--shell cmd|powershell|sh] [--timeout 60] [--max-output 100000] "
                  "[--allow <category,...>] [--force] [--no-exit-code] [--pace 2]")


def cmd_run_many(client, args):
    spec = {k: v for k, v in RUN_SPEC.items() if k not in ("--detach", "--job-hours")}
    spec["--pace"] = "float"
    opts, pos = parse_flags(args, spec, RUN_MANY_USAGE)
    if not pos or len(pos) > 2:
        die(RUN_MANY_USAGE, EXIT_USAGE)
    targets = parse_targets(pos[0])
    if not targets:
        die("ERROR: no targets\n" + RUN_MANY_USAGE, EXIT_USAGE)
    command, implied = command_source(pos[1] if len(pos) > 1 else None, opts["file"], RUN_MANY_USAGE)
    shell, timeout, max_output = run_opts(opts, RUN_MANY_USAGE)
    shell = shell or implied
    allow = parse_allow(opts["allow"])
    pace = opts["pace"] if opts["pace"] is not None else 2.0
    exit_code = not opts["no_exit_code"]
    refuse_if_denied(command, allow, opts["force"])  # once, before anything is sent

    rows = []
    for n, t in enumerate(targets):
        if n:
            time.sleep(pace)
        try:
            s, _ = pick_session(client, t, for_command=True)
        except TargetError as e:
            print("===== " + t + " =====")
            print("(not run) " + str(e).split("\n")[0])
            rows.append((t, "unresolved", None))
            audit("run-many", target=t, sha256=digest(command), chars=len(command),
                  status="unresolved")
            continue
        sid, name = s["SessionID"], s.get("Name", t)
        sh = default_shell(s, shell)
        res = execute(client, sid, wrap_command(command, sh, timeout, max_output, exit_code), timeout)
        audit("run-many", target=t, session=sid, name=name, shell=sh, sha256=digest(command),
              chars=len(command), allow=",".join(sorted(allow)) or None,
              force=opts["force"] or None, status=res["status"], exit=res["exit"])
        print("===== " + name + " (" + sid + ") =====")
        if res["output"]:
            print(res["output"])
        explain(res, name, timeout, exit_code)
        rows.append((name, res["status"], res["exit"]))

    ok = sum(1 for _, st, ex in rows if st == "ok" and not ex)
    print("")
    print("SUMMARY: " + str(len(rows)) + " target(s), " + str(ok) + " succeeded, "
          + str(len(rows) - ok) + " did not")
    width = max(len(r[0]) for r in rows)
    for name, st, ex in rows:
        detail = ("exit " + str(ex)) if st == "ok" and ex is not None else ""
        print("  " + name.ljust(width) + "  " + st.ljust(10) + " " + detail)
    return 0 if ok == len(rows) else 1


# --------------------------------------------------------------------- online

def cmd_online(client, args):
    usage = "usage: sc.py online [<sessionID|serial|machineName> ...] [--json]"
    opts, pos = parse_flags(args, {"--json": "bool"}, usage)

    def row(target, s, status):
        gi = (s or {}).get("GuestInfo") or {}
        return {"target": target, "status": status,
                "name": (s or {}).get("Name"), "session": (s or {}).get("SessionID"),
                "machine": gi.get("MachineName"), "user": gi.get("LoggedOnUserName"),
                "os": (gi.get("OperatingSystemName") or "").replace("Microsoft ", ""),
                "boot": (_when(gi.get("LastBootTime")) or "")[:16].replace("T", " "),
                "last_active": (last_active(s or {}) or "")[:16].replace("T", " ")}

    rows = []
    if not pos:
        found = client.call_json("GetSessionsByFilter",
                                 ["SessionType = 'Access' AND GuestConnectedCount > 0"]) or []
        found.sort(key=lambda s: (s.get("Name") or "").lower())
        rows = [row(s.get("Name"), s, "online") for s in found]
    else:
        for n, t in enumerate(pos):
            if n:
                time.sleep(0.3)
            try:
                s, _ = pick_session(client, t)
                rows.append(row(t, s, "online" if is_online(s) else "offline"))
            except TargetError:
                rows.append(row(t, None, "not found"))

    if opts["json"]:
        print(json.dumps(rows, indent=1))
    else:
        cols = [("target", "Target"), ("status", "Status"), ("user", "User"), ("os", "OS"),
                ("boot", "Last boot"), ("last_active", "Last active"), ("session", "SessionID")]
        if not pos:
            cols = [c for c in cols if c[0] not in ("status", "last_active")]
        table = [[h for _, h in cols]] + [[str(r[k] or "") for k, _ in cols] for r in rows]
        widths = [max(len(line[i]) for line in table) for i in range(len(cols))]
        for line in table:
            print("  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip())
        if not pos:
            print("\n" + str(len(rows)) + " online")
    return 0 if all(r["status"] == "online" for r in rows) else 1


# --------------------------------------------------------------------- push

PUSH_USAGE = ("usage: sc.py push <sessionID|serial|machineName> <localfile> <remotepath> "
              "[--overwrite] [--chunk-kb 16] [--max-mb 5] [--timeout 60]")
REMOTE_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\[^\\/]+[\\/])")


def ps_str(text):
    """A PowerShell expression that evaluates to `text`, immune to quoting and to
    anything between here and the endpoint that might eat backslashes."""
    return ("[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
            + base64.b64encode(text.encode("utf-8")).decode() + "'))")


def push_scripts(remote, data, chunk, overwrite):
    p = "$p = " + ps_str(remote) + "\n$part = $p + '.sc-part'\n"
    pre = (p + "if ((Test-Path -LiteralPath $p) -and -not $" + ("true" if overwrite else "false")
           + ") { 'SC_PUSH_EXISTS'; return }\n"
           "$dir = Split-Path -Parent $p\n"
           "if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }\n"
           "Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue\n"
           "[IO.File]::WriteAllBytes($part, [byte[]]@())\n'SC_PUSH_READY'\n")
    chunks = []
    for i in range(0, len(data), chunk):
        b64 = base64.b64encode(data[i:i + chunk]).decode()
        chunks.append(p + "$b = [Convert]::FromBase64String('" + b64 + "')\n"
                      "$f = [IO.File]::Open($part, [IO.FileMode]::Append, [IO.FileAccess]::Write)\n"
                      "try { $f.Write($b, 0, $b.Length) } finally { $f.Close() }\n"
                      "'SC_PUSH_OK ' + (Get-Item -LiteralPath $part).Length\n")
    sha = hashlib.sha256(data).hexdigest().upper()
    fin = (p + "$h = (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash\n"
           "if ($h -ne '" + sha + "') { Remove-Item -LiteralPath $part -Force; 'SC_PUSH_BADHASH ' + $h; return }\n"
           "Move-Item -LiteralPath $part -Destination $p -Force\n'SC_PUSH_DONE ' + $h\n")
    return pre, chunks, fin, sha


def cmd_push(client, args):
    opts, pos = parse_flags(args, {"--overwrite": "bool", "--chunk-kb": "int", "--max-mb": "float",
                                   "--timeout": "int"}, PUSH_USAGE)
    if len(pos) != 3:
        die(PUSH_USAGE, EXIT_USAGE)
    target, local, remote = pos
    if not REMOTE_PATH_RE.match(remote):
        die("ERROR: the remote path must be absolute (C:\\... or \\\\server\\share\\...): " + remote,
            EXIT_USAGE)
    chunk_kb = opts["chunk_kb"] or 16
    if not 1 <= chunk_kb <= 64:
        die("ERROR: --chunk-kb must be between 1 and 64", EXIT_USAGE)
    max_mb = opts["max_mb"] or 5.0
    timeout = opts["timeout"] or 60
    try:
        with open(local, "rb") as f:
            data = f.read()
    except OSError as e:
        die("ERROR: can't read " + local + ": " + str(e), EXIT_USAGE)
    if len(data) > max_mb * 1024 * 1024:
        die("ERROR: " + local + " is " + str(round(len(data) / 1048576, 1)) + " MB, over --max-mb "
            + str(max_mb) + ". Each chunk is one command, so large files are slow; host big files "
            "somewhere the endpoint can download from instead.", EXIT_USAGE)

    s = resolve(client, target, for_command=True)
    sid, name = s["SessionID"], s.get("Name", target)
    if not is_windows(s):
        die("ERROR: push needs a Windows endpoint (it uses PowerShell); " + name + " reports "
            + str((s.get("GuestInfo") or {}).get("OperatingSystemName")), EXIT_USAGE)
    pre, chunks, fin, sha = push_scripts(remote, data, chunk_kb * 1024, opts["overwrite"])
    fields = dict(target=target, session=sid, name=name, sha256=sha.lower(), bytes=len(data))

    def step(script, expect):
        res = execute(client, sid, wrap_command(script, "powershell", timeout, exit_code=False), timeout)
        if res["status"] != "ok":
            explain(res, name, timeout, False)
            audit("push", status=res["status"], **fields)
            die("ERROR: push to " + name + " stopped (" + res["status"] + "). Nothing was moved "
                "into place; a partial '.sc-part' file may remain.",
                EXIT_TARGET if res["status"] == "offline" else EXIT_TIMEOUT)
        m = re.search(r"SC_PUSH_(\w+) ?(\S*)", res["output"])
        if not m or m.group(1) not in expect:
            audit("push", status="unexpected-reply", **fields)
            die("ERROR: unexpected reply from " + name + ":\n" + res["output"][:1000])
        return m.group(1), m.group(2)

    kind, _ = step(pre, ("READY", "EXISTS"))
    if kind == "EXISTS":
        audit("push", status="exists", **fields)
        die("ERROR: " + remote + " already exists on " + name + ". Pass --overwrite to replace it.",
            EXIT_USAGE)
    for n, c in enumerate(chunks, 1):
        _, length = step(c, ("OK",))
        expected = min(len(data), n * chunk_kb * 1024)
        if str(expected) != length:
            audit("push", status="length-mismatch", **fields)
            die("ERROR: " + name + " reports " + length + " bytes after chunk " + str(n)
                + ", expected " + str(expected) + ". Nothing was moved into place.")
        if len(chunks) > 3:
            note("  chunk " + str(n) + "/" + str(len(chunks)))
    kind, got = step(fin, ("DONE", "BADHASH"))
    if kind == "BADHASH":
        audit("push", status="bad-hash", **fields)
        die("ERROR: hash mismatch on " + name + " (got " + got + ", expected " + sha
            + "). The partial file was deleted.")
    audit("push", status="ok", **fields)
    print("Pushed " + local + " -> " + name + ":" + remote + " (" + str(len(data))
          + " bytes, SHA-256 " + sha + " verified)")
    return 0


# --------------------------------------------------------------------- detach / job

JOB_ID_RE = re.compile(r"^sc-\d{8}-\d{6}-[0-9a-f]{4}$")
JOB_DIR_PS = """$root = Join-Path $env:ProgramData 'sc-toolkit'
$d = Join-Path $root 'jobs'
"""
# Job files live in a folder only SYSTEM and Administrators can write, so nobody
# else on the machine can swap a script before the SYSTEM task runs it. A folder
# someone else already owns is refused rather than reused.
JOB_DIR_SECURE_PS = """$trusted = 'S-1-5-18', 'S-1-5-32-544'
if (Test-Path -LiteralPath $root) {
  $owner = (Get-Acl -LiteralPath $root).GetOwner([Security.Principal.SecurityIdentifier]).Value
  if ($trusted -notcontains $owner) { 'SC_JOB_UNSAFE owner ' + $owner; return }
} else { New-Item -ItemType Directory -Path $root -Force | Out-Null }
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
foreach ($s in $trusted) {
  $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
    (New-Object Security.Principal.SecurityIdentifier $s), 'FullControl',
    'ContainerInherit,ObjectInherit', 'None', 'Allow')))
}
Set-Acl -LiteralPath $root -AclObject $acl
New-Item -ItemType Directory -Path $d -Force | Out-Null
"""


def new_job_id():
    return "sc-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def detach_script(job_id, command, shell, hours):
    if shell == "powershell":
        ext, payload = ".ps1", b"\xef\xbb\xbf" + command.encode("utf-8")  # BOM: PS 5.1 reads UTF-8
        invoke = ("& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $d '"
                  + job_id + ".ps1')")
    else:
        ext, payload = ".cmd", command.encode("utf-8")
        invoke = "& cmd.exe /c (Join-Path $d '" + job_id + ".cmd')"
    runner = (JOB_DIR_PS + "$log = Join-Path $d '" + job_id + ".log'\n$code = 0\n"
              "try {\n  " + invoke + " *>&1 | Out-File -FilePath $log -Encoding utf8 -Append -Width 400\n"
              "  $code = [int]$LASTEXITCODE\n"
              "} catch { $_ | Out-File -FilePath $log -Encoding utf8 -Append; $code = 1 }\n"
              "Set-Content -Path (Join-Path $d '" + job_id + ".exit') -Value $code\n"
              "Unregister-ScheduledTask -TaskName '" + job_id + "' -Confirm:$false -ErrorAction SilentlyContinue\n")
    runner_b = b"\xef\xbb\xbf" + runner.encode("utf-8")
    return (JOB_DIR_PS + JOB_DIR_SECURE_PS
            + "[IO.File]::WriteAllBytes((Join-Path $d '" + job_id + ext + "'), [Convert]::FromBase64String('"
            + base64.b64encode(payload).decode() + "'))\n"
            + "[IO.File]::WriteAllBytes((Join-Path $d '" + job_id + ".run.ps1'), [Convert]::FromBase64String('"
            + base64.b64encode(runner_b).decode() + "'))\n"
            + "$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy "
              "Bypass -File \"' + (Join-Path $d '" + job_id + ".run.ps1') + '\"')\n"
            + "$st = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours " + str(int(hours))
            + ") -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable\n"
            + "Register-ScheduledTask -TaskName '" + job_id + "' -Action $a -Settings $st -User 'SYSTEM' "
              "-RunLevel Highest -Force | Out-Null\n"
            + "Start-ScheduledTask -TaskName '" + job_id + "'\nStart-Sleep -Seconds 2\n"
            + "$t = Get-ScheduledTask -TaskName '" + job_id + "' -ErrorAction SilentlyContinue\n"
            + "'SC_JOB_STARTED ' + $(if ($t) { $t.State } else { 'finished' })\n")


def detach(client, ident, command, shell, allow, force, hours):
    refuse_if_denied(command, allow, force)
    s = resolve(client, ident, for_command=True)
    sid, name = s["SessionID"], s.get("Name", ident)
    if not is_windows(s):
        die("ERROR: --detach needs a Windows endpoint (it uses a scheduled task); " + name
            + " reports " + str((s.get("GuestInfo") or {}).get("OperatingSystemName")), EXIT_USAGE)
    shell = shell or "cmd"
    if shell not in ("cmd", "powershell"):
        die("ERROR: --detach supports --shell cmd or powershell", EXIT_USAGE)
    job_id = new_job_id()
    res = execute(client, sid, wrap_command(detach_script(job_id, command, shell, hours),
                                            "powershell", 60, exit_code=False), 60)
    m = re.search(r"SC_JOB_(STARTED|UNSAFE) ?(.*)", res["output"] or "")
    audit("detach", target=ident, session=sid, name=name, shell=shell, sha256=digest(command),
          chars=len(command), job=job_id, allow=",".join(sorted(allow)) or None,
          force=force or None, status=(m.group(1).lower() if m else res["status"]))
    if res["status"] != "ok" or not m:
        explain(res, name, 60, False)
        die("ERROR: couldn't start the job on " + name + ":\n" + (res["output"] or "")[:1000])
    if m.group(1) == "UNSAFE":
        die("ERROR: refused: %ProgramData%\\sc-toolkit on " + name + " is owned by "
            + m.group(2).strip() + ", not SYSTEM or Administrators, so another user could tamper "
            "with job files. Check that folder before using --detach.")
    print("Started job " + job_id + " on " + name + " (" + m.group(2).strip() + ").")
    print("Check it:  sc.py job " + sid + " " + job_id + "   (add --wait 600 to wait for it)")
    return 0


def job_script(job_id, tail, cleanup, force):
    base = JOB_DIR_PS + "$id = '" + job_id + "'\n"
    if cleanup:
        return (base + "$t = Get-ScheduledTask -TaskName $id -ErrorAction SilentlyContinue\n"
                "$done = Test-Path -LiteralPath (Join-Path $d \"$id.exit\")\n"
                "if ($t -and -not $done -and -not $" + ("true" if force else "false")
                + ") { 'SC_JOB_BUSY'; return }\n"
                "if ($t) { Stop-ScheduledTask -TaskName $id -ErrorAction SilentlyContinue; "
                "Unregister-ScheduledTask -TaskName $id -Confirm:$false -ErrorAction SilentlyContinue }\n"
                "Get-ChildItem -LiteralPath $d -Filter \"$id.*\" -ErrorAction SilentlyContinue | Remove-Item -Force\n"
                "'SC_JOB_CLEANED'\n")
    return (base + "$t = Get-ScheduledTask -TaskName $id -ErrorAction SilentlyContinue\n"
            "$ex = Join-Path $d \"$id.exit\"; $log = Join-Path $d \"$id.log\"\n"
            "if (Test-Path -LiteralPath $ex) { 'SC_JOB_STATE finished'; 'SC_JOB_EXIT ' + (Get-Content -LiteralPath $ex -Raw).Trim() }\n"
            "elseif ($t) { 'SC_JOB_STATE running ' + $t.State }\n"
            "elseif (Test-Path -LiteralPath (Join-Path $d \"$id.run.ps1\")) { 'SC_JOB_STATE lost' }\n"
            "else { 'SC_JOB_STATE unknown' }\n"
            "if (Test-Path -LiteralPath $log) { 'SC_JOB_LOGSIZE ' + (Get-Item -LiteralPath $log).Length; "
            "'SC_JOB_LOG'; Get-Content -LiteralPath $log -Tail " + str(int(tail)) + " }\n")


LIST_JOBS_PS = JOB_DIR_PS + """if (-not (Test-Path -LiteralPath $d)) { 'SC_JOB_NONE'; return }
Get-ChildItem -LiteralPath $d -Filter 'sc-*.run.ps1' | Sort-Object LastWriteTime | ForEach-Object {
  $id = $_.Name -replace '\\.run\\.ps1$', ''
  $ex = Join-Path $d "$id.exit"
  $state = if (Test-Path -LiteralPath $ex) { 'finished exit=' + (Get-Content -LiteralPath $ex -Raw).Trim() }
           elseif (Get-ScheduledTask -TaskName $id -ErrorAction SilentlyContinue) { 'running' } else { 'lost' }
  'SC_JOB ' + $id + ' ' + $state
}
"""


def cmd_job(client, args):
    usage = ("usage: sc.py job <sessionID|serial|machineName> <jobID> [--tail 40] [--wait <seconds>] "
             "[--cleanup [--force]]\n       sc.py job <sessionID|serial|machineName> --list")
    opts, pos = parse_flags(args, {"--tail": "int", "--wait": "int", "--cleanup": "bool",
                                   "--force": "bool", "--list": "bool"}, usage)
    if len(pos) != (1 if opts["list"] else 2):
        die(usage, EXIT_USAGE)
    if not opts["list"] and not JOB_ID_RE.match(pos[1]):
        die("ERROR: '" + pos[1] + "' isn't a job ID (they look like sc-20260101-120000-ab12)", EXIT_USAGE)
    s = resolve(client, pos[0], for_command=True)
    sid, name = s["SessionID"], s.get("Name", pos[0])

    def ask(script):
        res = execute(client, sid, wrap_command(script, "powershell", 60, exit_code=False), 60)
        if res["status"] != "ok":
            explain(res, name, 60, False)
            die("ERROR: couldn't read job state on " + name + " (" + res["status"] + ")",
                EXIT_TARGET if res["status"] == "offline" else EXIT_TIMEOUT)
        return res["output"] or ""

    if opts["list"]:
        jobs = re.findall(r"^SC_JOB (\S+) (.+)$", ask(LIST_JOBS_PS), re.M)
        if not jobs:
            print("No jobs on " + name + ".")
        for jid, state in jobs:
            print(jid + "  " + state.strip())
        return 0

    job_id = pos[1]
    if opts["cleanup"]:
        out = ask(job_script(job_id, 0, True, opts["force"]))
        busy = "SC_JOB_BUSY" in out
        audit("job-cleanup", target=pos[0], session=sid, name=name, job=job_id,
              force=opts["force"] or None, status="busy" if busy else "cleaned")
        if busy:
            die("ERROR: job " + job_id + " is still running. Wait for it, or add --force to stop it.",
                EXIT_USAGE)
        print("Removed job " + job_id + " from " + name + ".")
        return 0

    tail = opts["tail"] if opts["tail"] is not None else 40
    wait = opts["wait"] or 0
    deadline = time.time() + wait
    while True:
        out = ask(job_script(job_id, tail, False, False))
        st = re.search(r"^SC_JOB_STATE (\w+)", out, re.M)
        state = st.group(1) if st else "unknown"
        if state != "running" or time.time() >= deadline:
            break
        time.sleep(min(15, max(1, deadline - time.time())))
    ex = re.search(r"^SC_JOB_EXIT (-?\d+)", out, re.M)
    size = re.search(r"^SC_JOB_LOGSIZE (\d+)", out, re.M)
    print("# job " + job_id + " on " + name + ": " + state
          + (" (exit " + ex.group(1) + ")" if ex else "")
          + (", log " + size.group(1) + " bytes" if size else ", no log yet"))
    parts = re.split(r"^SC_JOB_LOG\s*$", out, maxsplit=1, flags=re.M)
    if len(parts) > 1 and parts[1].strip():
        print(parts[1].strip("\r\n"))
    if state == "running":
        return EXIT_TIMEOUT if wait else 0
    if state == "unknown":
        note("NOTE: there's no job " + job_id + " on " + name + ".")
        return EXIT_TARGET
    if state == "lost":
        note("NOTE: the job's task is gone but it never wrote an exit code (the machine may have "
             "restarted, or the job hit --job-hours).")
        return 1
    code = int(ex.group(1)) if ex else 0
    return code if 0 <= code <= 123 else 1


# --------------------------------------------------------------------- chat

def get_chat(client, sid, since=None):
    """Chat messages (tech EventType 45 / guest 71) for a session, oldest first.
    If `since` (ISO8601) is given, only messages at/after it are returned - compared on
    the first 19 chars (YYYY-MM-DDTHH:MM:SS) to sidestep fractional-second/zone mismatch."""
    cutoff = str(since)[:19] if since else None
    msgs = []
    for e in _events(client, sid):
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


def chat_command(client, ident, since=None):
    sid, name = resolve_session(client, ident, for_command=False)
    msgs = get_chat(client, sid, since=since)
    if not msgs:
        print("# no chat messages on session " + name + " (" + sid + ")"
              + (" since " + str(since) if since else ""))
        return 0
    lines = [f"[{t[:16].replace('T', ' ')}] {who}: {txt}" for t, who, txt in msgs]
    print("# chat transcript - " + name + " (" + sid + ")")
    print("\n".join(lines))
    return 0


def cmd_chat(client, args):
    usage = "usage: sc.py chat <sessionID|serial|machineName> [--since <iso8601>]"
    opts, pos = parse_flags(args, {"--since": "str"}, usage)
    if len(pos) != 1:
        die(usage, EXIT_USAGE)
    return chat_command(client, pos[0], since=opts["since"])


# --------------------------------------------------------------------- raw

def raw_call(client, method, args):
    if method in BLOCKED_METHODS:
        die("ERROR: " + method + " is blocked by policy in this plugin.", EXIT_USAGE)
    if method not in READ_METHODS + ACTION_METHODS:
        die("ERROR: unknown method " + method + ". Allowed: "
            + ", ".join(READ_METHODS + ACTION_METHODS)
            + ", or run, run-many, online, push, job, chat, setup.", EXIT_USAGE)
    body = []
    for raw in args:
        try:
            body.append(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            body.append(raw)
    if method == "SendCommandToSession" and len(body) > 1 and isinstance(body[1], str):
        if denied(body[1]):
            die("REFUSED: this command matches the denylist. Use 'sc.py run' instead, which "
                "explains the match and takes --allow/--force.", EXIT_REFUSED)
    if method in ACTION_METHODS:
        audit("raw:" + method, session=body[0] if body and isinstance(body[0], str) else None,
              sha256=digest(json.dumps(body[1:], sort_keys=True)))
    txt = client.call(method, body).strip()
    if not txt:
        print("OK (no content) - " + method + " succeeded")
        return 0
    try:
        print(json.dumps(json.loads(txt), indent=1, default=str))
    except (json.JSONDecodeError, ValueError):
        print(txt)
    return 0


COMMANDS = {"run": cmd_run, "run-many": cmd_run_many, "online": cmd_online,
            "push": cmd_push, "job": cmd_job, "chat": cmd_chat}


def main():
    utf8_stdio()
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if len(sys.argv) >= 2 else EXIT_USAGE
    if sys.argv[1] == "setup":
        do_setup(sys.argv[2:])
        return 0
    handler = COMMANDS.get(sys.argv[1])
    client = Client(find_config())
    if handler:
        return handler(client, sys.argv[2:])
    return raw_call(client, sys.argv[1], sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
