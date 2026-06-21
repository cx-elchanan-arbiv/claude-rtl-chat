#!/usr/bin/env python3
"""Scan Claude sessions and feed the RTL mirror.

Writes:
  * sessions.json  — lightweight index (id, project, snippet, mtime, turns, active).
  * s-<id>.md      — full RTL conversation per session, re-rendered ONLY when the
    transcript changed. Conversation text is markdown; tool actions (Edit/Write/
    Bash) and their outputs become collapsible <details> the page renders inline.

active  = touched within ACTIVE_SECONDS (terminals in use).
history = older, kept within HISTORY_SECONDS, for recovering a closed session.
"""
import glob
import html
import json
import os
import re
import subprocess
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


def live_counts():
    """Map sanitized project-dir -> number of live `claude` terminal processes
    whose cwd is that project. This is the real 'which terminals are open' signal
    (transcript mtime can't tell an idle-open session from a just-closed one).
    Returns {} if detection fails → caller falls back to the time window."""
    try:
        ps = subprocess.run(["ps", "-Ao", "pid,comm"],
                            capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return {}
    pids = [p.split()[0] for p in ps.splitlines()
            if p.strip().split()[1:2] == ["claude"]]
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


def render_assistant(content):
    out = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "text" and blk.get("text"):
            out.append(blk["text"].strip())
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


def render(path):
    """Render one transcript to markdown (+inline tool <details>)."""
    parts, last_user, first_user = [], "", ""
    tok_total, tok_out = 0, 0
    speaker = None  # 'user' | 'assistant'

    def header(role):
        return "### 🧑 אתה" if role == "user" else "### 🤖 Claude"

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
                continue
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
                    clean = IMG_MARK.sub("🖼️", text)
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
    turns = sum(1 for p in parts if p.startswith("### "))
    clean = IMG_MARK.sub("", last_user)
    snippet = " ".join(clean.split())[:40] or "(תמונה)"
    ftitle = " ".join(IMG_MARK.sub("", first_user).split())[:38] or "(תמונה)"
    return md, turns, snippet, tok_total, tok_out, ftitle


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main():
    now = time.time()
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    files = [f for f in files if now - os.path.getmtime(f) <= HISTORY_SECONDS]
    files.sort(key=os.path.getmtime, reverse=True)
    files = files[:MAX_SESSIONS]

    cache, new_cache = load_cache(), {}
    sessions, keep = [], set()
    live = live_counts()      # {projdir: open-terminal count}; files are mtime-desc
    used = {}                 # how many we've marked active per project so far
    try:
        with open(OWNED, encoding="utf-8") as fh:
            owned = json.load(fh)          # {id: {created, title}}
    except Exception:
        owned = {}
    seen_ids = set()

    for path in files:
        sid = os.path.splitext(os.path.basename(path))[0]
        mt = int(os.path.getmtime(path))
        mdname = f"s-{sid}.md"
        mdpath = os.path.join(BASE, mdname)
        keep.add(mdname)

        c = cache.get(sid)
        if c and c.get("mtime") == mt and os.path.exists(mdpath) and "title" in c:
            snippet, turns = c["snippet"], c["turns"]
            tokens, out, title = c["tokens"], c.get("out", 0), c["title"]
        else:
            try:
                md, turns, snippet, tokens, out, title = render(path)
            except Exception:
                continue
            write_atomic(mdpath, md)

        # active = an open `claude` terminal in this project (top-K by recency);
        # fall back to the time window if process detection found nothing.
        projdir = os.path.basename(os.path.dirname(path))
        if live:
            k = live.get(projdir, 0)
            is_active = used.get(projdir, 0) < k
            if is_active:
                used[projdir] = used.get(projdir, 0) + 1
        else:
            is_active = (now - mt) <= ACTIVE_SECONDS
        if sid in owned:
            is_active = True   # browser chats stay "open" even when no process runs

        new_cache[sid] = {"mtime": mt, "snippet": snippet, "turns": turns,
                          "tokens": tokens, "out": out, "title": title}
        seen_ids.add(sid)
        sessions.append({
            "id": sid, "short": sid[:8],
            "project": project_label(os.path.dirname(path)),
            "snippet": snippet, "title": title, "mtime": mt, "turns": turns,
            "tokens": tokens, "out": out,
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
            "tokens": 0, "out": 0, "active": True, "owned": True, "md": None,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)

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
