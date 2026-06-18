#!/usr/bin/env python3
"""V2 — bidirectional RTL chat. Independent copy of the mirror + a compose layer.

  * thread:   re-render sessions every second   (extract.main)
  * thread:   refresh Max usage every 5 minutes (usage.main, best-effort)
  * main:     serve this folder over http://127.0.0.1:7778
  * POST /new   -> create a browser-owned session id (no claude yet)
  * POST /send  -> run `claude -p` for {id,text}: --session-id (first) / --resume
                   (continue), serialized per id. Reply lands in the transcript and
                   the 1s render loop shows it RTL.

Fully self-contained — shares no files with V1; both only READ ~/.claude/projects.
"""
import glob
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 7778
OWNED = os.path.join(BASE, "owned.json")
PROJECTS = os.path.expanduser("~/.claude/projects")

# Where browser-started chats run. Change to a project path to let Read/Grep see it.
DEFAULT_CWD = os.path.expanduser("~")

# Permission level for browser-run Claude. SAFE default: read/plan/answer only.
# Full power: replace with ["--permission-mode", "acceptEdits"] or
# ["--dangerously-skip-permissions"].
PERM = ["--allowedTools", "Read", "Grep", "Glob", "WebFetch", "WebSearch", "TodoWrite"]

os.chdir(BASE)
sys.path.insert(0, BASE)
import extract  # noqa: E402

_owned_lock = threading.Lock()
_locks = {}            # id -> Lock (serialize sends per session)
_locks_guard = threading.Lock()


def _lock_for(sid):
    with _locks_guard:
        if sid not in _locks:
            _locks[sid] = threading.Lock()
        return _locks[sid]


def read_owned():
    try:
        with open(OWNED, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_owned(d):
    tmp = OWNED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.replace(tmp, OWNED)


def transcript_exists(sid):
    return bool(glob.glob(os.path.join(PROJECTS, "*", f"{sid}.jsonl")))


def run_claude(sid, text):
    """Blocking claude -p run; reply is written to the transcript by Claude Code."""
    first = not transcript_exists(sid)
    sess = ["--session-id", sid] if first else ["--resume", sid]
    cmd = ["claude", "-p", text, *sess, *PERM]
    try:
        r = subprocess.run(cmd, cwd=DEFAULT_CWD, capture_output=True, text=True,
                           timeout=600)
        if r.returncode != 0:
            print(f"[send {sid[:8]}] rc={r.returncode} {r.stderr[:300]}", flush=True)
    except Exception as e:
        print(f"[send {sid[:8]}] error: {e}", flush=True)


def handle_send(sid, text):
    with _lock_for(sid):   # one run per session at a time
        run_claude(sid, text)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad json"})

        if self.path == "/new":
            sid = str(uuid.uuid4())
            with _owned_lock:
                d = read_owned()
                d[sid] = {"created": int(time.time()), "title": "שיחה חדשה"}
                write_owned(d)
            return self._json(200, {"id": sid})

        if self.path == "/send":
            sid, text = body.get("id"), (body.get("text") or "").strip()
            if not sid or not text:
                return self._json(400, {"error": "missing id/text"})
            with _owned_lock:                      # only browser-owned chats are writable
                if sid not in read_owned():
                    return self._json(403, {"error": "not an owned session"})
            threading.Thread(target=handle_send, args=(sid, text), daemon=True).start()
            return self._json(200, {"status": "started"})

        self._json(404, {"error": "not found"})


class Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def extract_loop():
    while True:
        try:
            extract.main()
        except Exception as e:
            print("extract error:", e, flush=True)
        time.sleep(1)


def usage_loop():
    try:
        import usage
    except Exception:
        return
    while True:
        try:
            usage.main()
        except Exception as e:
            print("usage error:", e, flush=True)
        time.sleep(300)


def main():
    threading.Thread(target=extract_loop, daemon=True).start()
    threading.Thread(target=usage_loop, daemon=True).start()
    print(f"RTL chat (V2) serving on http://127.0.0.1:{PORT}", flush=True)
    Server(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
