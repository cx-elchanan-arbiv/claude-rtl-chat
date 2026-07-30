#!/usr/bin/env python3
"""Scan Claude sessions and feed the RTL mirror.

Writes:
  * sessions.json  — lightweight index (id, project, snippet, last, turns, active,
    origin, bg).
  * s-<id>.md      — full RTL conversation per session, re-rendered ONLY when the
    transcript changed. Conversation text is markdown; tool actions (Edit/Write/
    Bash) and their outputs become collapsible <details> the page renders inline.

active  = held open by a live claude session process (see live_session_ids).
history = everything else, kept within HISTORY_SECONDS, for recovering a closed session.

last    = timestamp of the last REAL message, not the file's mtime. Claude Code appends
  metadata-only lines (ai-title / agent-name / mode, none of them timestamped) whenever
  its daemon re-claims an old background job, which bumps mtime without adding a word to
  the conversation. Sorting/displaying by mtime therefore floated long-dead jobs to the
  top of the strip wearing today's clock (2026-07-30: three tabs whose last real message
  was 10-12 July). Everything user-facing keys on `last`; mtime stays only as the
  cheap "did the file change" signal for the render cache and the HISTORY_SECONDS sweep.
origin  = who opened it: "web" (this UI, `claude -p` → entrypoint sdk-cli), "terminal"
  (entrypoint cli, incl. background jobs), or "mixed" (a terminal session we adopted and
  then wrote to). Drives the 3-way view filter in the page.
bg      = it's a background job (`sessionKind: "bg"` in the transcript), i.e. an agent
  the terminal spawned — not a chat you sat in front of.
"""
import datetime
import glob
import html
import json
import os
import re
import subprocess
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(BASE, "sessions.json")
CACHE = os.path.join(BASE, "_cache.json")
OWNED = os.path.join(BASE, "owned.json")   # sessions this V2 chat created/manages
PROJECTS = os.path.expanduser("~/.claude/projects")

ACTIVE_SECONDS = 30 * 60
HISTORY_SECONDS = 7 * 24 * 3600
MAX_SESSIONS = 30
MAX_RESULT_CHARS = 4000
IMG_MARK = re.compile(r"\[Image[^\]]*\]")


def project_label(project_dir):
    name = os.path.basename(project_dir)
    return name.rstrip("-").split("-")[-1] or name


# Plumbing the Claude Code daemon keeps around: pre-warmed PTY hosts / spares, and the
# daemon itself. They all run with argv[0] == "claude", they OUTLIVE the terminal window
# by design, and every claimed spare leaves a ~/.claude/sessions/<pid>.json still pointing
# at the session it once hosted — so counting them as live sessions marked long-finished
# background jobs "open in a terminal" (2026-07-30: tabs from 10-12 July shown as live).
HELPER_MARKERS = ("bg-spare", "bg-pty-host", "daemon run")
# What a real session process looks like: the bare `claude` launcher, or the versioned
# binary it execs (~/.local/share/claude/versions/<v>). The old check compared ps's `comm`
# field to "claude" — but macOS prints comm as a full PATH, so it matched ONLY argv[0]
# == "claude" (the helpers above + terminals started by typing `claude`) and silently
# missed every session running as the versioned binary. Matching on the command line
# fixes both halves: the helpers drop out, the real sessions come in.
CLAUDE_PATH_MARKS = ("/claude/versions/",)


def _claude_procs():
    """pid -> full command line for every live process. None when ps is unusable."""
    try:
        ps = subprocess.run(["ps", "-Ao", "pid=,command="],
                            capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return None
    procs = {}
    for line in ps.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit() and cmd.strip():
            procs[pid] = cmd.strip()
    return procs


def _claude_pids():
    """PIDs of live claude SESSION processes — daemon plumbing deliberately excluded.
    Returns None when we can't tell (no ps), so callers can fall back."""
    procs = _claude_procs()
    if procs is None:
        return None
    pids = []
    for pid, cmd in procs.items():
        argv0 = cmd.split(None, 1)[0]
        is_claude = (os.path.basename(argv0) == "claude"
                     or any(m in argv0 for m in CLAUDE_PATH_MARKS))
        if is_claude and not any(m in cmd for m in HELPER_MARKERS):
            pids.append(pid)
    return pids


SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")


def live_session_ids():
    """PRECISE 'which sessions are live in a terminal' signal: Claude Code writes
    ~/.claude/sessions/<pid>.json for every running process, recording its exact
    sessionId + cwd. We read those and keep only the ones whose pid is still a live
    claude SESSION process per _claude_pids (the files linger after a crash, and the
    daemon's spares hold on to theirs long after the job they hosted finished), giving
    the precise set of session ids currently open in a terminal.

    Returns that set, or None when we can't tell — no `ps`, can't read the dir, or
    claude is running yet no session file matches (the layout changed) — so the
    caller falls back to live_counts(). An empty set means: ps worked, no claude
    terminals → nothing is live."""
    pids = _claude_pids()
    if pids is None:
        return None
    if not pids:
        return set()                 # definitively nothing live
    live_pids = set(pids)
    try:
        files = os.listdir(SESSIONS_DIR)
    except Exception:
        return None
    ids, matched = set(), False
    for fn in files:
        if not fn.endswith(".json") or fn[:-len(".json")] not in live_pids:
            continue                 # not a session file, or stale (pid not running)
        matched = True
        try:
            with open(os.path.join(SESSIONS_DIR, fn), encoding="utf-8") as fh:
                sid = (json.load(fh) or {}).get("sessionId")
        except Exception:
            continue
        if sid:
            ids.add(sid)
    return ids if matched else None  # no file matched a live pid → let caller fall back


def live_counts():
    """FALLBACK 'which terminals are open' signal: map sanitized project-dir ->
    number of live `claude` processes whose cwd is that project. Used only when
    live_session_ids() can't pin the exact ids. Note: a count alone can't say
    *which* sessions are live, so the caller approximates with the most-recent K
    per project — which is why the precise signal above is preferred.
    Returns {} if detection fails → caller falls back to the time window."""
    pids = _claude_pids()
    if not pids:
        return {}
    counts = {}
    for pid in pids:
        try:
            out = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                                capture_output=True, text=True, timeout=4).stdout
        except Exception:
            continue
        cwd = next((ln[1:] for ln in out.splitlines() if ln.startswith("n")), None)
        if cwd:
            key = cwd.replace("/", "-")
            counts[key] = counts.get(key, 0) + 1
    return counts


def esc(s):
    return html.escape(str(s))


def md_text(s):
    """Neutralize raw HTML written INSIDE conversation prose, so a literal tag typed
    in the text (e.g. Claude explaining `<code dir="ltr">` while discussing RTL) can't
    open a real DOM element and swallow the rest of the page. Escaping only `<` keeps
    all Markdown intact (**bold**, lists, `code`, #headers) while disarming every tag
    (no `<` → no tag). extract's OWN structural html (headers, tool <details>) is added
    separately and stays live — this touches only the untrusted message text."""
    return str(s).replace("<", "&lt;")


def details(summary_html, body, cls="tool", is_open=False):
    # collapse to ONE physical line (encode newlines) so a blank line inside a
    # diff can't make marked terminate the HTML block early; <pre> + &#10; still
    # renders real line breaks in the browser.
    one = esc(body).replace("\r", "").replace("\n", "&#10;")
    op = " open" if is_open else ""
    return (f'\n\n<details class="{cls}"{op}><summary>{summary_html}</summary>'
            f'<pre>{one}</pre></details>\n\n')


def kv_body(inp):
    """Readable input dump that KEEPS real newlines (json.dumps escapes them to
    literal \\n, which renders as garbage). Strings stay raw; dict/list -> indent."""
    parts = []
    for k, v in inp.items():
        if isinstance(v, str):
            parts.append(f"{k}:\n{v}" if "\n" in v else f"{k}: {v}")
        else:
            parts.append(f"{k}: {json.dumps(v, ensure_ascii=False, indent=2)}")
    return "\n".join(parts)


def summarize_tool(name, inp):
    """Return (summary_html, body_text, is_open, cls) for one tool_use."""
    if name in ("Edit", "NotebookEdit"):
        base = os.path.basename(inp.get("file_path", "") or "")
        body = f'- {inp.get("old_string", "")}\n+ {inp.get("new_string", "")}'
        return f'🔧 ערך <code>{esc(base)}</code>', body, False, "tool"
    if name == "Write":
        base = os.path.basename(inp.get("file_path", "") or "")
        return f'📄 כתב <code>{esc(base)}</code>', inp.get("content", ""), False, "tool"
    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        head = desc or " ".join(cmd.split())[:50]
        return f'💻 <code>{esc(head)}</code>', cmd, False, "tool"
    if name == "Read":
        base = os.path.basename(inp.get("file_path", "") or "")
        return f'📖 קרא <code>{esc(base)}</code>', kv_body(inp), False, "tool"
    # human-text tools → render readable + expanded (like the terminal)
    if name == "ExitPlanMode":
        return '📋 תוכנית', inp.get("plan", ""), True, "tool plan"
    if name == "AskUserQuestion":
        lines = []
        for q in (inp.get("questions") or []):
            lines.append("❓ " + (q.get("question", "")))
            for o in (q.get("options") or []):
                lab, desc = o.get("label", ""), o.get("description", "")
                lines.append(f"  • {lab}" + (f" — {desc}" if desc else ""))
            lines.append("")
        return '❓ שאלה', "\n".join(lines).strip(), True, "tool plan"
    if name == "TodoWrite":
        mark = {"completed": "✓", "in_progress": "▸", "pending": "☐"}
        lines = [f"{mark.get(t.get('status',''), '•')} {t.get('content', t.get('activeForm',''))}"
                 for t in (inp.get("todos") or [])]
        return '✓ משימות', "\n".join(lines), False, "tool"
    return f'🔧 {esc(name)}', kv_body(inp), False, "tool"


def result_text(blk):
    c = blk.get("content", "")
    if isinstance(c, list):
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return str(c)


# Claude Code writes this exact text into the transcript when a turn ends with no
# reply of its own (e.g. it finished via tool calls and added no closing prose). It's
# benign — NOT an error — but raw it reads like "Claude refused to answer". Show a calm,
# clear note instead so it doesn't look broken.
NO_REPLY_TEXT = "No response requested."
NO_REPLY_HTML = ('<div class="noreply">↳ Claude סיים את התור בלי הודעת טקסט '
                 '(בדרך כלל אחרי הרצת כלים). זה תקין — אפשר להמשיך.</div>')


def render_assistant(content):
    out = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "text" and blk.get("text"):
            t = blk["text"].strip()
            out.append(NO_REPLY_HTML if t == NO_REPLY_TEXT else md_text(t))
        elif blk.get("type") == "tool_use":
            summ, body, is_open, cls = summarize_tool(blk.get("name", ""), blk.get("input", {}) or {})
            out.append(details(summ, body, cls=cls, is_open=is_open))
    return "\n\n".join(p for p in out if p)


def split_user(content):
    """Return (real_text, [result_bodies]) for a user message."""
    if isinstance(content, str):
        t = content.strip()
        return ("" if t.startswith("<") else t), []
    texts, results = [], []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "text" and blk.get("text"):
            t = blk["text"].strip()
            if t and not t.startswith("<"):
                texts.append(t)
        elif blk.get("type") == "tool_result":
            results.append(result_text(blk))
    return "\n\n".join(texts), results


def ts_epoch(iso):
    """'2026-07-12T14:07:54.713Z' -> epoch seconds, or None if it isn't a timestamp.
    Only real messages carry one; the metadata lines a daemon re-claim appends don't."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def render(path):
    """Render one transcript to markdown (+inline tool <details>).

    Also reports, from the same single pass: `last` (epoch of the newest real message),
    `origin` ("web"/"terminal"/"mixed", from each message's entrypoint) and `bg` (is this
    a background job) — see the module docstring for why those don't come from the file."""
    parts, last_user, first_user = [], "", ""
    tok_total, tok_out = 0, 0
    last_ts, seen_web, seen_term, is_bg = 0, False, False, False
    speaker = None  # 'user' | 'assistant'

    def header(role):
        # raw <h3> with an explicit data-role so the renderer keys on THIS, not on a
        # text sniff — Claude's own `### heading` inside a reply then stays inline
        # content instead of being mistaken for a turn boundary, and a heading that
        # merely contains the substring "אתה" (e.g. "נראתה") can't become a fake user
        # bubble. This is extract's own trusted html, added outside the escaped text.
        return ('<h3 data-role="user">🧑 אתה</h3>' if role == "user"
                else '<h3 data-role="assistant">🤖 Claude</h3>')

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            role = event.get("type")
            if role not in ("user", "assistant"):
                continue                      # metadata line — never counts as activity
            ts = ts_epoch(event.get("timestamp"))
            if ts and ts > last_ts:
                last_ts = ts
            ep = event.get("entrypoint")
            if ep == "sdk-cli":               # our own `claude -p` — a message from this UI
                seen_web = True
            elif ep == "cli":                 # typed in a terminal, or a bg job it spawned
                seen_term = True
            if event.get("sessionKind") == "bg":
                is_bg = True
            content = event.get("message", {}).get("content", [])

            if role == "assistant":
                u = event.get("message", {}).get("usage", {}) or {}
                out = u.get("output_tokens", 0) or 0
                tok_out += out
                tok_total += ((u.get("input_tokens", 0) or 0) + out +
                              (u.get("cache_creation_input_tokens", 0) or 0) +
                              (u.get("cache_read_input_tokens", 0) or 0))
                seg = render_assistant(content)
                if not seg:
                    continue
                if speaker != "assistant":
                    parts.append(header("assistant"))
                    speaker = "assistant"
                parts.append(seg)
            else:
                text, results = split_user(content)
                if text:
                    clean = IMG_MARK.sub("🖼️", md_text(text))
                    if speaker != "user":
                        parts.append(header("user"))
                        speaker = "user"
                    parts.append(clean)
                    last_user = text
                    if not first_user:
                        first_user = text
                # tool outputs attach under the current (assistant) block, no new header
                for r in results:
                    if r.strip():
                        parts.append(details("↳ פלט", r[:MAX_RESULT_CHARS], cls="tool result"))

    md = "\n\n".join(parts) if parts else "*ממתין לתשובה הראשונה…*"
    turns = sum(1 for p in parts if p.startswith('<h3 data-role='))
    clean = IMG_MARK.sub("", last_user)
    snippet = " ".join(clean.split())[:40] or "(תמונה)"
    ftitle = " ".join(IMG_MARK.sub("", first_user).split())[:38] or "(תמונה)"
    # unknown entrypoint (old transcripts) -> "terminal": not ours until proven otherwise.
    origin = ("mixed" if seen_web and seen_term else "web" if seen_web else "terminal")
    return md, turns, snippet, tok_total, tok_out, ftitle, last_ts, origin, is_bg


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_atomic(path, text):
    # unique temp name in the SAME dir (os.replace is atomic only within a filesystem).
    # A fixed "<path>.tmp" was racy: main() is called from serve.py's 1s loop AND its
    # /new,/adopt,/close handler threads, so two concurrent writers to the same .tmp
    # could truncate each other and publish a half-written file. mkstemp gives each its own.
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".wtmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


_main_lock = threading.Lock()   # serialize whole-scan runs (1s loop + handler-triggered)


def main():
    # one whole-scan at a time — serve.py fires this from its 1s loop and from the
    # /new,/adopt,/close handlers concurrently; without this they'd race on the shared
    # sessions.json / _cache.json writes (and redo the same work).
    with _main_lock:
        _run()


def _run():
    now = time.time()
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    # mtime on purpose here: it's the only signal available without reading the file, so
    # it stays the cheap prefilter/cap. A daemon-touched old job therefore still enters the
    # index — and then sorts by its REAL last message, i.e. way down in history.
    files = [f for f in files if now - os.path.getmtime(f) <= HISTORY_SECONDS]
    files.sort(key=os.path.getmtime, reverse=True)
    files = files[:MAX_SESSIONS]

    cache, new_cache = load_cache(), {}
    sessions, keep = [], set()
    live_ids = live_session_ids()   # exact set of sids live in a terminal (or None)
    live = live_counts() if live_ids is None else {}   # fallback count, only if needed
    used = {}                 # how many we've marked active per project so far
    try:
        with open(OWNED, encoding="utf-8") as fh:
            owned = json.load(fh)          # {id: {created, title}}
    except Exception:
        owned = {}
    seen_ids = set()

    for path in files:
        sid = os.path.splitext(os.path.basename(path))[0]
        st = os.stat(path)
        mt = int(st.st_mtime)                       # whole seconds — display / sort / active window
        sig = f"{st.st_mtime_ns}:{st.st_size}"      # precise change key: catches sub-second writes
        mdname = f"s-{sid}.md"
        mdpath = os.path.join(BASE, mdname)
        keep.add(mdname)

        c = cache.get(sid)
        # "last" in c doubles as the cache version: an entry written before this field
        # existed re-renders once instead of publishing a session with no real timestamp.
        if c and c.get("sig") == sig and os.path.exists(mdpath) and "last" in c:
            snippet, turns = c["snippet"], c["turns"]
            tokens, out, title = c["tokens"], c.get("out", 0), c["title"]
            last, origin, bg = c["last"], c["origin"], c["bg"]
        else:
            try:
                md, turns, snippet, tokens, out, title, last, origin, bg = render(path)
            except Exception:
                continue
            write_atomic(mdpath, md)
        last = last or mt      # transcript has no timestamped message at all → file time

        # active = this exact session is held open by a live claude session process.
        # Fallbacks when the precise signal is unavailable: an open terminal in this
        # project (top-K by recency), else "spoke within ACTIVE_SECONDS".
        projdir = os.path.basename(os.path.dirname(path))
        if live_ids is not None:
            is_active = sid in live_ids
        elif live:
            k = live.get(projdir, 0)
            is_active = used.get(projdir, 0) < k
            if is_active:
                used[projdir] = used.get(projdir, 0) + 1
        else:
            is_active = (now - last) <= ACTIVE_SECONDS   # last real message, not mtime
        if sid in owned:
            is_active = True   # browser chats stay "open" even when no process runs

        new_cache[sid] = {"sig": sig, "mtime": mt, "snippet": snippet, "turns": turns,
                          "tokens": tokens, "out": out, "title": title,
                          "last": last, "origin": origin, "bg": bg}
        seen_ids.add(sid)
        sessions.append({
            "id": sid, "short": sid[:8],
            "project": project_label(os.path.dirname(path)),
            "snippet": snippet, "title": title, "mtime": mt, "turns": turns,
            "tokens": tokens, "out": out, "last": last, "origin": origin, "bg": bg,
            "active": is_active, "owned": sid in owned, "md": mdname,
        })

    # owned chats with no transcript yet (just created via /new) → placeholder tab
    for oid, meta in owned.items():
        if oid in seen_ids:
            continue
        sessions.append({
            "id": oid, "short": oid[:8],
            "project": os.path.basename((meta.get("cwd") or "chat").rstrip("/")) or "chat",
            "snippet": "", "title": meta.get("title", "שיחה חדשה"),
            "mtime": meta.get("created", int(now)), "turns": 0,
            "tokens": 0, "out": 0, "last": meta.get("created", int(now)),
            "origin": "web", "bg": False,
            "active": True, "owned": True, "md": None,
        })
    sessions.sort(key=lambda s: s["last"], reverse=True)   # real activity, not file mtime

    write_atomic(INDEX, json.dumps({"generated": int(now), "sessions": sessions},
                                   ensure_ascii=False))
    write_atomic(CACHE, json.dumps(new_cache, ensure_ascii=False))

    for f in glob.glob(os.path.join(BASE, "s-*.md")):
        if os.path.basename(f) not in keep:
            try:
                os.remove(f)
            except OSError:
                pass


if __name__ == "__main__":
    main()
