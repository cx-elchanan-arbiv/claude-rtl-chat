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
import base64
import glob
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 7778
OWNED = os.path.join(BASE, "owned.json")
PROJECTS = os.path.expanduser("~/.claude/projects")

# launchd runs with a minimal PATH, so resolve claude absolutely + give it a real PATH.
CLAUDE_BIN = (os.path.expanduser("~/.local/bin/claude")
              if os.path.exists(os.path.expanduser("~/.local/bin/claude"))
              else (shutil.which("claude") or "claude"))
CHILD_PATH = (os.path.expanduser("~/.local/bin") + ":/opt/homebrew/bin:"
              "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")

# Where browser-started chats run by default (can be overridden per chat in the UI).
DEFAULT_CWD = os.path.expanduser("~")
PROJECTS_PARENT = os.path.expanduser("~/Projects")   # offered in the dir picker
UPLOADS = os.path.join(BASE, "uploads")              # pasted/attached files land here
os.makedirs(UPLOADS, exist_ok=True)

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

_status = {}           # id -> {state, started, detail, tokens} live "working" indicator
_status_lock = threading.Lock()


def set_status(sid, **kw):
    with _status_lock:
        s = _status.setdefault(sid, {})
        for k, v in kw.items():
            if v is not None:
                s[k] = v


def clear_status(sid):
    with _status_lock:
        _status.pop(sid, None)


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
    """Stream claude -p; reply still lands in the transcript (rendered by the mirror),
    while stdout stream-json events drive the live 'working' indicator (_status)."""
    cwd = (read_owned().get(sid) or {}).get("cwd") or DEFAULT_CWD
    if not os.path.isdir(cwd):
        cwd = DEFAULT_CWD
    first = not transcript_exists(sid)
    sess = ["--session-id", sid] if first else ["--resume", sid]
    cmd = [CLAUDE_BIN, "-p", text, *sess, "--add-dir", UPLOADS,
           "--output-format", "stream-json", "--include-partial-messages", "--verbose", *PERM]
    env = os.environ.copy()
    env["PATH"] = CHILD_PATH
    set_status(sid, state="thinking", started=time.time(), detail=None, tokens=0)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, env=env)
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") != "stream_event":
                continue
            ev = e.get("event") or {}
            et = ev.get("type")
            if et == "content_block_start":
                cb = ev.get("content_block") or {}
                bt = cb.get("type")
                if bt == "thinking":
                    set_status(sid, state="thinking", detail=None)
                elif bt == "text":
                    set_status(sid, state="responding", detail=None)
                elif bt == "tool_use":
                    set_status(sid, state="tool", detail=cb.get("name"))
            elif et == "message_delta":
                u = ev.get("usage") or {}
                if u.get("output_tokens"):
                    set_status(sid, tokens=u["output_tokens"])
        proc.wait(timeout=600)
        if proc.returncode not in (0, None):
            print(f"[send {sid[:8]}] rc={proc.returncode}", flush=True)
    except Exception as e:
        print(f"[send {sid[:8]}] error: {e}", flush=True)
    finally:
        clear_status(sid)


def handle_send(sid, text):
    with _lock_for(sid):   # one run per session at a time
        run_claude(sid, text)


def list_dirs():
    """Folders offered in the new-chat picker: home + ~/Projects/* subdirs."""
    out = [{"path": os.path.expanduser("~"), "label": "🏠 בית (~)"}]
    try:
        for name in sorted(os.listdir(PROJECTS_PARENT)):
            p = os.path.join(PROJECTS_PARENT, name)
            if os.path.isdir(p) and not name.startswith("."):
                out.append({"path": p, "label": "📁 " + name})
    except Exception:
        pass
    return out


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

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/dirs":
            return self._json(200, {"dirs": list_dirs()})
        if p == "/status":
            with _status_lock:
                return self._json(200, dict(_status))
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad json"})

        if self.path == "/new":
            sid = str(uuid.uuid4())
            cwd = body.get("cwd") or DEFAULT_CWD
            if not os.path.isdir(cwd):
                cwd = DEFAULT_CWD
            with _owned_lock:
                d = read_owned()
                d[sid] = {"created": int(time.time()), "title": "שיחה חדשה", "cwd": cwd}
                write_owned(d)
            try:
                extract.main()   # surface the placeholder immediately (no 1s wait)
            except Exception:
                pass
            return self._json(200, {"id": sid, "cwd": cwd})

        if self.path == "/upload":
            name = os.path.basename(body.get("name") or "file")
            data = body.get("data") or ""
            if "," in data:                       # strip data: URL prefix if present
                data = data.split(",", 1)[1]
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file"
            dest = os.path.join(UPLOADS, f"{uuid.uuid4().hex[:8]}_{safe}")
            try:
                with open(dest, "wb") as fh:
                    fh.write(base64.b64decode(data))
            except Exception as e:
                return self._json(400, {"error": f"bad upload: {e}"})
            return self._json(200, {"path": dest, "name": name})

        if self.path == "/close":
            sid = body.get("id")
            with _owned_lock:
                d = read_owned()
                if sid in d:
                    del d[sid]            # no longer browser-owned → drops to history
                    write_owned(d)
            try:
                extract.main()
            except Exception:
                pass
            return self._json(200, {"status": "closed"})

        if self.path == "/send":
            sid, text = body.get("id"), (body.get("text") or "").strip()
            files = body.get("files") or []
            if not sid or (not text and not files):
                return self._json(400, {"error": "missing id/text"})
            with _owned_lock:                      # only browser-owned chats are writable
                if sid not in read_owned():
                    return self._json(403, {"error": "not an owned session"})
            if files:
                refs = ", ".join(files)
                text = (text + f"\n\n[קבצים מצורפים — קרא אותם עם הכלי Read: {refs}]").strip()
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
