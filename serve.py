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

# Permission levels for browser-run Claude, chosen per-session (stored in owned.json).
# Headless `claude -p` can't prompt, so each level is a fixed set of pre-auth flags
# (forms per the official Claude Code docs: headless / permission-modes / permissions).
#   read = SAFE default: read/search/web only (allowedTools whitelist, comma-separated)
#   edit = auto-accept file edits + common fs commands (acceptEdits) — no arbitrary Bash
#   full = skip all prompts incl. Bash (bypassPermissions — the documented official flag);
#          also the only level that gets --chrome (headless browser control), since write
#          browser actions need bypassPermissions to clear the extension's consent gate.
# Gotcha: in -p mode a built-in tool that isn't pre-authed ABORTS the turn (no prompt).
#   MCP-server tools (e.g. Claude in Chrome) differ — a denied call returns an error
#   result the model recovers from, and read-only MCP tools bypass the allowedTools
#   whitelist — which is why --chrome is gated to "full" only (see run_claude).
PERM_MODES = {
    "read": ["--allowedTools", "Read,Grep,Glob,WebFetch,WebSearch,TodoWrite"],
    "edit": ["--permission-mode", "acceptEdits"],
    "full": ["--permission-mode", "bypassPermissions"],
}
DEFAULT_PERM = "read"
# When adopting a terminal-started session, map its recorded permissionMode back
# onto our 3 levels (default/plan and anything unknown → the safe "read").
PERM_FROM_MODE = {"acceptEdits": "edit", "bypassPermissions": "full"}

# Model per browser-run chat (stored per-session in owned.json), mirroring perm.
# Aliases track the latest of each tier (`claude --help`: --model accepts an alias);
# "default" = no flag, i.e. let Claude Code use its own configured default. Only
# browser-owned chats are switchable — a mirrored terminal session runs under whatever
# model that terminal chose; we only read its transcript, so we can't set it.
MODELS = {"opus": ["--model", "opus"], "sonnet": ["--model", "sonnet"],
          "haiku": ["--model", "haiku"], "fable": ["--model", "fable"]}
DEFAULT_MODEL = "opus"   # also the fallback for any legacy chat whose stored model is gone

os.chdir(BASE)
sys.path.insert(0, BASE)
import extract  # noqa: E402

_owned_lock = threading.Lock()
_locks = {}            # id -> Lock (serialize sends per session)
_locks_guard = threading.Lock()

_status = {}           # id -> {state, started, detail, tokens} live "working" indicator
_status_lock = threading.Lock()

_procs = {}            # id -> running claude Popen (for /stop)
_procs_lock = threading.Lock()


def heal_transcript(sid):
    """Strip trailing invalid-JSON line(s) a prior kill may have left, so --resume
    doesn't choke. (Documented corruption risk of killing claude -p mid-write.)"""
    path = next(iter(glob.glob(os.path.join(PROJECTS, "*", f"{sid}.jsonl"))), None)
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return
    changed = False
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop(); changed = True; continue
        try:
            json.loads(last); break
        except Exception:
            lines.pop(); changed = True
    if changed:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            print(f"[heal {sid[:8]}] trimmed trailing invalid line(s)", flush=True)
        except Exception:
            pass


def set_status(sid, **kw):
    with _status_lock:
        s = _status.setdefault(sid, {})
        for k, v in kw.items():
            if v is not None:
                s[k] = v


def clear_status(sid):
    with _status_lock:
        _status.pop(sid, None)


def interrupted_path(sid):
    return os.path.join(BASE, f"s-{sid}.interrupted")


def clear_interrupted(sid):
    try:
        os.remove(interrupted_path(sid))
    except OSError:
        pass


def save_interrupted(sid, text):
    """Safety net: when a turn dies mid-reply (API error / kill / crash), the streamed
    text was only ever live in /status and never reached the transcript. Persist it so
    the browser can still show it instead of silently losing the reply."""
    text = (text or "").strip()
    if not text:
        return
    try:
        with open(interrupted_path(sid), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


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


def session_meta_from_transcript(sid):
    """Read (cwd, permissionMode) from a session's transcript — every Claude Code
    event records its cwd, so an adopted terminal session resumes in the right dir."""
    path = next(iter(glob.glob(os.path.join(PROJECTS, "*", f"{sid}.jsonl"))), None)
    cwd = perm = None
    if not path:
        return cwd, perm
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                cwd = cwd or e.get("cwd")
                perm = perm or e.get("permissionMode")
                if cwd and perm:
                    break
    except Exception:
        pass
    return cwd, perm


def run_claude(sid, text):
    """Stream claude -p; reply still lands in the transcript (rendered by the mirror),
    while stdout stream-json events drive the live 'working' indicator (_status)."""
    owned = read_owned().get(sid) or {}
    cwd = owned.get("cwd") or DEFAULT_CWD
    if not os.path.isdir(cwd):
        cwd = DEFAULT_CWD
    perm_flags = PERM_MODES.get(owned.get("perm"), PERM_MODES[DEFAULT_PERM])
    first = not transcript_exists(sid)
    if not first:
        heal_transcript(sid)   # safety net: a prior /stop may have left a half-written line
    sess = ["--session-id", sid] if first else ["--resume", sid]
    # --chrome (the "Claude in Chrome" bridge) only for "full" sessions: it's the only
    # level whose bypassPermissions can actually drive the browser headlessly. In
    # read/edit, write browser actions hit the extension's own "requires permission"
    # gate anyway, so we skip loading the 22 browser tools into their context (and the
    # bridge-connect cost) — verified: --chrome works fine in headless `claude -p`.
    chrome_flag = ["--chrome"] if owned.get("perm") == "full" else []
    model_flags = MODELS.get(owned.get("model"), MODELS[DEFAULT_MODEL])   # per-chat model; built each turn → live switch
    cmd = [CLAUDE_BIN, "-p", text, *sess, "--add-dir", UPLOADS,
           "--output-format", "stream-json", "--include-partial-messages", "--verbose",
           *model_flags, *chrome_flag, *perm_flags]
    env = os.environ.copy()
    env["PATH"] = CHILD_PATH
    set_status(sid, state="thinking", started=time.time(), detail=None, tokens=0)
    clear_interrupted(sid)   # this retry supersedes any earlier crashed attempt
    partial = [""]   # live assistant text for this turn, streamed to the browser via /status
    ok = False       # did the turn finish cleanly? if not, we keep the streamed text
    try:
        # start_new_session: run claude in its OWN process group/session so a restart
        # of THIS server (launchd kickstart of com.elchanan.rtl-chat) only kills serve.py
        # and leaves the in-flight turn running to finish writing the transcript — instead
        # of killing the reply mid-write. (Classic self-restart footgun: asking the chat to
        # restart its own server used to nuke the very turn that issued the command.) /stop
        # still works: we SIGTERM the proc directly, not via serve.py's group.
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, env=env,
                                start_new_session=True)
        with _procs_lock:
            _procs[sid] = proc
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
            if et == "message_start":
                partial[0] = ""
                set_status(sid, partial="")
            elif et == "content_block_start":
                cb = ev.get("content_block") or {}
                bt = cb.get("type")
                if bt == "thinking":
                    set_status(sid, state="thinking", detail=None)
                elif bt == "text":
                    set_status(sid, state="responding", detail=None)
                elif bt == "tool_use":
                    set_status(sid, state="tool", detail=cb.get("name"))
            elif et == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    partial[0] += delta["text"]
                    set_status(sid, partial=partial[0])
            elif et == "message_delta":
                u = ev.get("usage") or {}
                if u.get("output_tokens"):
                    set_status(sid, tokens=u["output_tokens"])
        proc.wait(timeout=600)
        ok = proc.returncode in (0, None)
        if not ok:
            print(f"[send {sid[:8]}] rc={proc.returncode}", flush=True)
    except Exception as e:
        print(f"[send {sid[:8]}] error: {e}", flush=True)
    finally:
        with _procs_lock:
            _procs.pop(sid, None)
        if not ok:                       # crashed/killed mid-reply → don't lose the text
            save_interrupted(sid, partial[0])
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

    def _cors(self):
        # Allow cross-origin calls from other local apps (e.g. SubsTranslator on :80).
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight (browsers send this before a JSON POST cross-origin).
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

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
            perm = body.get("perm")
            if perm not in PERM_MODES:
                perm = DEFAULT_PERM
            model = body.get("model")
            if model not in MODELS:
                model = DEFAULT_MODEL
            with _owned_lock:
                d = read_owned()
                d[sid] = {"created": int(time.time()), "title": "שיחה חדשה",
                          "cwd": cwd, "perm": perm, "model": model}
                write_owned(d)
            try:
                extract.main()   # surface the placeholder immediately (no 1s wait)
            except Exception:
                pass
            return self._json(200, {"id": sid, "cwd": cwd, "perm": perm})

        if self.path == "/perm":                  # switch a chat's permission level mid-conversation
            sid = body.get("id")
            perm = body.get("perm")
            if perm not in PERM_MODES:
                return self._json(400, {"error": "bad perm"})
            with _owned_lock:
                d = read_owned()
                if sid not in d:                  # only browser-owned chats are switchable
                    return self._json(403, {"error": "not an owned session"})
                d[sid]["perm"] = perm
                write_owned(d)
            return self._json(200, {"status": "ok", "perm": perm})

        if self.path == "/model":                  # switch a chat's model mid-conversation
            sid = body.get("id")
            model = body.get("model")
            if model not in MODELS:
                return self._json(400, {"error": "bad model"})
            with _owned_lock:
                d = read_owned()
                if sid not in d:                  # only browser-owned chats are switchable
                    return self._json(403, {"error": "not an owned session"})
                d[sid]["model"] = model
                write_owned(d)
            return self._json(200, {"status": "ok", "model": model})

        if self.path == "/adopt":                  # take over a terminal-started session
            sid = body.get("id")
            if not sid or not transcript_exists(sid):
                return self._json(404, {"error": "no such session"})
            with _owned_lock:
                if sid in read_owned():
                    return self._json(200, {"status": "already-owned"})
            cwd, mode = session_meta_from_transcript(sid)
            if not cwd or not os.path.isdir(cwd):
                cwd = DEFAULT_CWD
            # safety: a live `claude` terminal in this project + a browser send would
            # have two processes resume the same transcript. Warn unless forced.
            if not body.get("force"):
                try:
                    if extract.live_counts().get(cwd.replace("/", "-"), 0) > 0:
                        return self._json(409, {"warning": "live-terminal", "cwd": cwd})
                except Exception:
                    pass
            perm = PERM_FROM_MODE.get(mode, DEFAULT_PERM)
            with _owned_lock:
                d = read_owned()
                d[sid] = {"created": int(time.time()), "title": "שיחה מאומצת",
                          "cwd": cwd, "perm": perm, "adopted": True}
                write_owned(d)
            try:
                extract.main()   # surface it as owned (writable + closable) at once
            except Exception:
                pass
            return self._json(200, {"status": "adopted", "cwd": cwd, "perm": perm})

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
            clear_interrupted(sid)        # drop any saved crashed-reply text
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

        if self.path == "/dismiss-interrupted":   # user acknowledged the crashed-reply block
            clear_interrupted(body.get("id"))
            return self._json(200, {"status": "ok"})

        if self.path == "/stop":
            sid = body.get("id")
            with _procs_lock:
                proc = _procs.get(sid)
            if proc and proc.poll() is None:
                proc.terminate()              # SIGTERM (graceful); run loop will clean up
                return self._json(200, {"status": "stopping"})
            return self._json(200, {"status": "not-running"})

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
