"""A small stand-in for the RESTful API Manager extension, for tests.

It keeps sessions and their event lists in memory and answers the handful of
methods sc.py uses. SendCommandToSession hands the command text to a responder
function, which returns the output to post as a command-output event (or None
to post nothing, like a machine that never answers).
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILTER_RE = re.compile(r"^(\w+) = '((?:[^']|'')*)'$")
FIELD = {"GuestMachineName": ("GuestInfo", "MachineName"),
         "GuestMachineSerialNumber": ("GuestInfo", "MachineSerialNumber"),
         "Name": (None, "Name")}


def make_session(name, sid, online=True, os_name="Microsoft Windows 11 Pro", serial="SN-" + "0" * 4):
    return {"Name": name, "SessionID": sid, "SessionType": 2,
            "ActiveConnections": [{"ProcessType": 2}] if online else [],
            "GuestInfo": {"MachineName": name, "MachineSerialNumber": serial,
                          "OperatingSystemName": os_name, "LoggedOnUserName": "user1",
                          "LastBootTime": "2026-01-01T08:00:00Z",
                          "LastActivityTime": "2026-01-02T09:00:00Z"}}


class Stub:
    def __init__(self):
        self.sessions = []
        self.events = {}
        self.commands = []      # (sid, text) in order sent
        self.calls = []         # method names in order
        self.responder = lambda sid, text: "ok"
        self.fail = []          # queue of (method, status) to fail once each
        self.empty_lookups = 0  # answer this many lookups with [] before answering properly
        self.after_command = None  # callback(stub, sid) run after a command is received
        self._eid = 0
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add(self, session):
        self.sessions.append(session)
        self.events.setdefault(session["SessionID"], [])
        return session

    def set_online(self, sid, online):
        for s in self.sessions:
            if s["SessionID"] == sid:
                s["ActiveConnections"] = [{"ProcessType": 2}] if online else []

    def post_event(self, sid, etype, data):
        with self._lock:
            self._eid += 1
            self.events[sid].append({"EventID": "e%d" % self._eid, "EventType": etype,
                                     "Data": data, "Time": "2026-01-02T10:00:00Z"})

    # ------------------------------------------------------------------
    def _lookup(self, expr):
        if expr == "SessionType = 'Access' AND GuestConnectedCount > 0":
            return [s for s in self.sessions if s["ActiveConnections"]]
        m = FILTER_RE.match(expr)
        if not m:
            return []
        field, value = m.group(1), m.group(2).replace("''", "'")
        parent, key = FIELD.get(field, (None, field))
        out = []
        for s in self.sessions:
            src = s.get(parent, {}) if parent else s
            if str(src.get(key, "")).lower() == value.lower():
                out.append(s)
        return out

    def handle(self, method, body):
        self.calls.append(method)
        for i, (m, status) in enumerate(self.fail):
            if m == method:
                del self.fail[i]
                return status, {"error": "stub failure"}
        if method in ("GetSessionBySessionID", "GetSessionsByFilter") and self.empty_lookups:
            self.empty_lookups -= 1
            return 200, []
        if method == "GetSessionBySessionID":
            return 200, [s for s in self.sessions if s["SessionID"] == body[0]]
        if method in ("GetSessionsByFilter",):
            return 200, self._lookup(body[0])
        if method == "GetSessionsByName":
            return 200, [s for s in self.sessions if s["Name"] == body[0]]
        if method == "GetSessionDetailsBySessionID":
            return 200, {"Events": list(self.events.get(body[0], []))}
        if method == "SendCommandToSession":
            sid, text = body[0], body[1]
            self.commands.append((sid, text))
            out = self.responder(sid, text)
            if out is not None:
                self.post_event(sid, 70, out)
            if self.after_command:
                self.after_command(self, sid)
            return 200, None
        return 200, None

    def _handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"[]")
                status, payload = stub.handle(method, body)
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "0")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H
