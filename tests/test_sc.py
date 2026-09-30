"""Tests for sc.py against a local stub of the RESTful API Manager extension.

Run from the repo root:  python -m unittest discover -s tests -v
No live ScreenConnect instance, network access or third-party packages needed.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "skills", "screenconnect", "scripts")
SC = os.path.join(SCRIPTS, "sc.py")
sys.path.insert(0, HERE)
sys.path.insert(0, SCRIPTS)

import sc  # noqa: E402
from stub import Stub, make_session  # noqa: E402

SID_A = "11111111-1111-1111-1111-111111111111"
SID_B = "22222222-2222-2222-2222-222222222222"
SID_C = "33333333-3333-3333-3333-333333333333"


def echo_exit(code=0, text="hello"):
    """Responder: print `text`, then the exit-code line if sc.py asked for one."""
    def responder(sid, cmd):
        return text + ("\n" + sc.EXIT_MARK + str(code) if sc.EXIT_MARK in cmd else "")
    return responder


class Base(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.stub.add(make_session("PC-ONE", SID_A))
        self.stub.responder = echo_exit()
        self.tmp = tempfile.mkdtemp()
        self.audit = os.path.join(self.tmp, "audit.log")

    def tearDown(self):
        self.stub.close()

    def sc(self, *args, stdin=None, env=None):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("SC_", "CLAUDE_PLUGIN_OPTION_SC_"))}
        e.update({"SC_URL": self.stub.url, "SC_AUTH_SECRET": "test-secret",
                  "SC_POLL_INTERVAL": "0.05", "SC_RETRY_BASE": "0.01",
                  "SC_AUDIT_LOG": self.audit})
        e.update(env or {})
        return subprocess.run([sys.executable, SC] + list(args), input=stdin, env=e,
                              capture_output=True, text=True, encoding="utf-8", timeout=60)

    def sent(self):
        return self.stub.commands[-1][1]


class TestRun(Base):
    def test_run_basic_and_directives(self):
        r = self.sc("run", "PC-ONE", "ipconfig")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hello", r.stdout)
        self.assertNotIn(sc.EXIT_MARK, r.stdout)
        cmd = self.sent()
        self.assertIn("#timeout=60000", cmd)
        self.assertIn("#maxlength=100000", cmd)
        self.assertIn("@echo " + sc.EXIT_MARK + "%ERRORLEVEL%", cmd)

    def test_powershell_prefix_and_exit_line(self):
        self.sc("run", "PC-ONE", "Get-Date", "--shell", "powershell", "--timeout", "30",
                "--max-output", "2000")
        cmd = self.sent()
        self.assertTrue(cmd.startswith("#!ps\n#timeout=30000\n#maxlength=2000\n"))
        self.assertIn("$__sc_ok = $?", cmd)

    def test_remote_exit_code_passes_through(self):
        self.stub.responder = echo_exit(3)
        r = self.sc("run", "PC-ONE", "whatever")
        self.assertEqual(r.returncode, 3)
        self.assertIn("remote exit code: 3", r.stderr)

    def test_no_exit_code(self):
        self.sc("run", "PC-ONE", "hostname", "--no-exit-code")
        self.assertNotIn(sc.EXIT_MARK, self.sent())

    def test_file_keeps_backslashes_and_implies_powershell(self):
        path = os.path.join(self.tmp, "t.ps1")
        with open(path, "w", encoding="utf-8-sig") as f:  # with a BOM, as some editors write
            f.write("Test-Path '\\\\server\\share'\n")
        r = self.sc("run", "PC-ONE", "--file", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        cmd = self.sent()
        self.assertIn("Test-Path '\\\\server\\share'", cmd)
        self.assertTrue(cmd.startswith("#!ps\n"))
        self.assertNotIn("\ufeff", cmd)

    def test_stdin(self):
        r = self.sc("run", "PC-ONE", "-", stdin="echo from-stdin\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("echo from-stdin", self.sent())

    def test_inline_and_file_together_is_usage_error(self):
        path = os.path.join(self.tmp, "x.cmd")
        open(path, "w").close()
        r = self.sc("run", "PC-ONE", "dir", "--file", path)
        self.assertEqual(r.returncode, sc.EXIT_USAGE)

    def test_non_windows_defaults_to_sh(self):
        self.stub.add(make_session("MAC-ONE", SID_B, os_name="macOS 15.1"))
        self.sc("run", "MAC-ONE", "uname -a")
        self.assertIn('echo "' + sc.EXIT_MARK + '$?"', self.sent())

    def test_unicode_output_on_legacy_code_page(self):
        self.stub.responder = echo_exit(text="left\u200eright \u2192 done")
        r = self.sc("run", "PC-ONE", "x", env={"PYTHONIOENCODING": "cp1252"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("done", r.stdout)

    def test_truncation_and_kill_are_reported(self):
        self.stub.responder = lambda sid, cmd: "partial\nTruncated output at 5000 characters."
        r = self.sc("run", "PC-ONE", "x")
        self.assertIn("truncated the output at 5000", r.stderr)
        self.stub.responder = lambda sid, cmd: "Killed after 10000 milliseconds."
        r = self.sc("run", "PC-ONE", "x")
        self.assertEqual(r.returncode, sc.EXIT_TIMEOUT)
        self.assertIn("KILLED", r.stderr)

    def test_timeout_while_online(self):
        self.stub.responder = lambda sid, cmd: None
        r = self.sc("run", "PC-ONE", "x", "--timeout", "1")
        self.assertEqual(r.returncode, sc.EXIT_TIMEOUT)
        self.assertIn("still online", r.stderr)

    def test_machine_goes_offline_while_waiting(self):
        self.stub.responder = lambda sid, cmd: None
        self.stub.after_command = lambda stub, sid: stub.set_online(sid, False)
        r = self.sc("run", "PC-ONE", "x", "--timeout", "1")
        self.assertEqual(r.returncode, sc.EXIT_TARGET)
        self.assertIn("OFFLINE", r.stderr)

    def test_bad_numbers_and_missing_values(self):
        self.assertEqual(self.sc("run", "PC-ONE", "x", "--timeout", "soon").returncode, sc.EXIT_USAGE)
        self.assertEqual(self.sc("run", "PC-ONE", "x", "--timeout").returncode, sc.EXIT_USAGE)
        self.assertEqual(self.sc("chat", "PC-ONE", "--since").returncode, sc.EXIT_USAGE)
        self.assertEqual(self.sc("run", "PC-ONE", "x", "--bogus").returncode, sc.EXIT_USAGE)


class TestResolution(Base):
    def test_apostrophe_in_name(self):
        self.stub.add(make_session("O'BRIEN-PC", SID_B))
        r = self.sc("run", "O'BRIEN-PC", "hostname")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stub.commands[-1][0], SID_B)

    def test_empty_lookup_is_retried(self):
        self.stub.empty_lookups = 2
        r = self.sc("run", SID_A, "hostname")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_not_found(self):
        r = self.sc("run", "NOPE-PC", "hostname")
        self.assertEqual(r.returncode, sc.EXIT_TARGET)
        self.assertIn("no session found", r.stderr)

    def test_ambiguous(self):
        self.stub.add(make_session("PC-ONE", SID_B))
        r = self.sc("run", "PC-ONE", "hostname")
        self.assertEqual(r.returncode, sc.EXIT_TARGET)
        self.assertIn("AMBIGUOUS", r.stderr)

    def test_http_429_is_retried(self):
        self.stub.fail = [("GetSessionsByFilter", 429)]
        r = self.sc("run", "PC-ONE", "hostname")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_send_command_not_retried_on_502(self):
        self.stub.fail = [("SendCommandToSession", 502)]
        r = self.sc("run", "PC-ONE", "hostname")
        self.assertEqual(r.returncode, sc.EXIT_ERROR)
        self.assertEqual(self.stub.calls.count("SendCommandToSession"), 1)


class TestDenylist(unittest.TestCase):
    def cats(self, text, allow=()):
        return {c for c, _, _ in sc.denied(text, allow)}

    def test_categories(self):
        cases = {
            "shutdown /r /t 0": "power",
            "Restart-Computer -Force": "power",
            "format C: /q": "disk",
            "Remove-Item C:\\Temp\\x -Recurse -Force": "delete",
            "del /s /q C:\\x": "delete",
            "vssadmin delete shadows /all": "backups",
            "bcdedit /set safeboot minimal": "boot",
            "reg delete HKLM\\Software\\X /f": "registry",
            "net user bob P@ss /add": "accounts",
            "Set-ExecutionPolicy Unrestricted": "execpolicy",
            "Set-MpPreference -DisableRealtimeMonitoring $true": "defender",
            "Add-MpPreference -ExclusionPath C:\\x": "defender",
            "sc stop WinDefend": "defender",
            "Set-NetFirewallProfile -All -Enabled False": "firewall",
            "netsh advfirewall set allprofiles state off": "firewall",
            "Disable-NetFirewallRule -DisplayGroup 'Remote Desktop'": "firewall",
            "Set-ItemProperty 'HKLM:\\x' -Name fDenyTSConnections -Value 0": "rdp",
            "reg add HKLM\\x /v fDenyTSConnections /t REG_DWORD /d 0 /f": "rdp",
            "sc.exe delete SomeService": "services",
            "wevtutil cl Security": "logs",
            "Clear-EventLog -LogName System": "logs",
        }
        for text, cat in cases.items():
            self.assertIn(cat, self.cats(text), text)

    def test_benign_commands_pass(self):
        for text in ["ipconfig /all", "Get-Service WinDefend",
                     "Set-MpPreference -DisableRealtimeMonitoring $false",
                     "Set-ItemProperty 'HKLM:\\x' -Name fDenyTSConnections -Value 1",
                     "del C:\\stuff\\file.txt", "Get-NetFirewallRule -Enabled True",
                     "Set-NetFirewallProfile -All -Enabled True", "net user bob"]:
            self.assertEqual(self.cats(text), set(), text)

    def test_allow_skips_only_named_category(self):
        text = "Restart-Computer; wevtutil cl System"
        self.assertEqual(self.cats(text, allow={"power"}), {"logs"})


class TestDenylistCli(Base):
    def test_refusal_names_category_and_allow_works(self):
        r = self.sc("run", "PC-ONE", "shutdown /r /t 60")
        self.assertEqual(r.returncode, sc.EXIT_REFUSED)
        self.assertIn("[power]", r.stderr)
        self.assertIn("--allow power", r.stderr)
        self.assertEqual(self.stub.commands, [])
        r = self.sc("run", "PC-ONE", "shutdown /r /t 60", "--allow", "power")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unknown_allow_category(self):
        self.assertEqual(self.sc("run", "PC-ONE", "x", "--allow", "nope").returncode, sc.EXIT_USAGE)

    def test_raw_send_command_is_checked(self):
        r = self.sc("SendCommandToSession", SID_A, "shutdown /s")
        self.assertEqual(r.returncode, sc.EXIT_REFUSED)
        self.assertEqual(self.stub.commands, [])


class TestRunMany(Base):
    def test_summary_and_exit(self):
        self.stub.add(make_session("PC-TWO", SID_B))
        self.stub.add(make_session("PC-OFF", SID_C, online=False))
        r = self.sc("run-many", "PC-ONE,PC-TWO,PC-OFF,PC-GONE", "hostname", "--pace", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SUMMARY: 4 target(s), 2 succeeded, 2 did not", r.stdout)
        self.assertEqual([c[0] for c in self.stub.commands], [SID_A, SID_B])

    def test_all_ok_and_list_file(self):
        self.stub.add(make_session("PC-TWO", SID_B))
        lst = os.path.join(self.tmp, "targets.txt")
        with open(lst, "w") as f:
            f.write("PC-ONE  # first\n\nPC-TWO\npc-one\n")
        r = self.sc("run-many", "@" + lst, "hostname", "--pace", "0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.stub.commands), 2)

    def test_denylist_checked_before_anything_is_sent(self):
        r = self.sc("run-many", "PC-ONE", "Restart-Computer", "--pace", "0")
        self.assertEqual(r.returncode, sc.EXIT_REFUSED)
        self.assertEqual(self.stub.commands, [])


class TestOnline(Base):
    def test_list_all_online(self):
        self.stub.add(make_session("PC-OFF", SID_C, online=False))
        r = self.sc("online")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PC-ONE", r.stdout)
        self.assertNotIn("PC-OFF", r.stdout)

    def test_named_targets(self):
        self.stub.add(make_session("PC-OFF", SID_C, online=False))
        r = self.sc("online", "PC-ONE", "PC-OFF", "PC-GONE", "--json")
        rows = {x["target"]: x["status"] for x in json.loads(r.stdout)}
        self.assertEqual(rows, {"PC-ONE": "online", "PC-OFF": "offline", "PC-GONE": "not found"})
        self.assertEqual(r.returncode, 1)


class PushEndpoint:
    """Plays the endpoint side of `push`: decodes each script and keeps the file."""

    def __init__(self):
        self.files = {}

    def __call__(self, sid, cmd):
        path = base64.b64decode(re.search(r"FromBase64String\('([^']*)'\)\)", cmd).group(1)).decode()
        part = path + ".sc-part"
        if "'SC_PUSH_READY'" in cmd:
            overwrite = "-not $true" in cmd
            if path in self.files and not overwrite:
                return "SC_PUSH_EXISTS"
            self.files[part] = b""
            return "SC_PUSH_READY"
        if "SC_PUSH_OK" in cmd:
            chunk = re.search(r"\$b = \[Convert\]::FromBase64String\('([^']*)'\)", cmd).group(1)
            self.files[part] += base64.b64decode(chunk)
            return "SC_PUSH_OK " + str(len(self.files[part]))
        expected = re.search(r"if \(\$h -ne '([0-9A-F]+)'\)", cmd).group(1)
        got = hashlib.sha256(self.files[part]).hexdigest().upper()
        if got != expected:
            del self.files[part]
            return "SC_PUSH_BADHASH " + got
        self.files[path] = self.files.pop(part)
        return "SC_PUSH_DONE " + got


class TestPush(Base):
    def test_push_round_trip(self):
        ep = PushEndpoint()
        self.stub.responder = ep
        data = os.urandom(40000)
        local = os.path.join(self.tmp, "payload.bin")
        with open(local, "wb") as f:
            f.write(data)
        remote = "C:\\ProgramData\\Test Folder\\payload.bin"
        r = self.sc("push", "PC-ONE", local, remote, "--chunk-kb", "16")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(ep.files[remote], data)
        self.assertEqual(len(self.stub.commands), 1 + 3 + 1)  # preflight, 3 chunks, verify
        r = self.sc("push", "PC-ONE", local, remote)
        self.assertEqual(r.returncode, sc.EXIT_USAGE)
        self.assertIn("already exists", r.stderr)
        r = self.sc("push", "PC-ONE", local, remote, "--overwrite")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_relative_remote_path_refused(self):
        local = os.path.join(self.tmp, "a.txt")
        open(local, "w").close()
        self.assertEqual(self.sc("push", "PC-ONE", local, "a.txt").returncode, sc.EXIT_USAGE)

    def test_size_cap(self):
        local = os.path.join(self.tmp, "big.bin")
        with open(local, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024))
        r = self.sc("push", "PC-ONE", local, "C:\\x.bin", "--max-mb", "1")
        self.assertEqual(r.returncode, sc.EXIT_USAGE)
        self.assertEqual(self.stub.commands, [])


class TestDetachAndJob(Base):
    def test_detach_writes_payload_and_runner(self):
        self.stub.responder = lambda sid, cmd: "SC_JOB_STARTED Running"
        r = self.sc("run", "PC-ONE", "Get-Date", "--shell", "powershell", "--detach")
        self.assertEqual(r.returncode, 0, r.stderr)
        job = re.search(r"Started job (sc-\d{8}-\d{6}-[0-9a-f]{4})", r.stdout).group(1)
        cmd = self.sent()
        self.assertIn("Register-ScheduledTask -TaskName '" + job + "'", cmd)
        self.assertIn("SetAccessRuleProtection", cmd)
        payload = re.search(r"'" + job + r"\.ps1'\), \[Convert\]::FromBase64String\('([^']*)'", cmd).group(1)
        self.assertEqual(base64.b64decode(payload), b"\xef\xbb\xbfGet-Date")

    def test_detach_is_denylist_checked(self):
        r = self.sc("run", "PC-ONE", "Restart-Computer", "--detach")
        self.assertEqual(r.returncode, sc.EXIT_REFUSED)
        self.assertEqual(self.stub.commands, [])

    def test_detach_refuses_unsafe_folder(self):
        self.stub.responder = lambda sid, cmd: "SC_JOB_UNSAFE owner S-1-5-21-1-2-3-1001"
        r = self.sc("run", "PC-ONE", "Get-Date", "--detach")
        self.assertEqual(r.returncode, sc.EXIT_ERROR)
        self.assertIn("tamper", r.stderr)

    def test_job_finished(self):
        self.stub.responder = lambda sid, cmd: ("SC_JOB_STATE finished\nSC_JOB_EXIT 4\n"
                                                "SC_JOB_LOGSIZE 11\nSC_JOB_LOG\nline1\nline2")
        r = self.sc("job", "PC-ONE", "sc-20260101-120000-ab12")
        self.assertEqual(r.returncode, 4)
        self.assertIn("finished (exit 4)", r.stdout)
        self.assertIn("line2", r.stdout)

    def test_job_id_validated(self):
        r = self.sc("job", "PC-ONE", "../../evil")
        self.assertEqual(r.returncode, sc.EXIT_USAGE)
        self.assertEqual(self.stub.commands, [])

    def test_job_cleanup_busy(self):
        self.stub.responder = lambda sid, cmd: "SC_JOB_BUSY"
        r = self.sc("job", "PC-ONE", "sc-20260101-120000-ab12", "--cleanup")
        self.assertEqual(r.returncode, sc.EXIT_USAGE)


class TestAudit(Base):
    def test_audit_has_hash_not_text(self):
        secret_cmd = "net use Z: \\\\srv\\share Sup3rS3cret /user:someone"
        self.sc("run", "PC-ONE", secret_cmd)
        with open(self.audit, encoding="utf-8") as f:
            rec = json.loads(f.readlines()[-1])
        self.assertEqual(rec["sha256"], hashlib.sha256(secret_cmd.encode()).hexdigest())
        self.assertEqual(rec["session"], SID_A)
        self.assertEqual(rec["status"], "ok")
        with open(self.audit, encoding="utf-8") as f:
            self.assertNotIn("Sup3rS3cret", f.read())

    def test_audit_off(self):
        self.sc("run", "PC-ONE", "hostname", env={"SC_AUDIT_LOG": "off"})
        self.assertFalse(os.path.exists(self.audit))


class TestUnits(unittest.TestCase):
    def test_split_exit(self):
        self.assertEqual(sc.split_exit("a\nb\n__SC_EXIT__=7\n"), ("a\nb", 7))
        self.assertEqual(sc.split_exit("no marker"), ("no marker", None))

    def test_quote(self):
        self.assertEqual(sc.q("O'BRIEN"), "'O''BRIEN'")

    def test_parse_targets(self):
        self.assertEqual(sc.parse_targets("a, b,,A  c"), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
