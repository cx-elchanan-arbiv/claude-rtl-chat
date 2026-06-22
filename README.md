# Claude RTL Chat 🪞💬

A local web app that **mirrors AND drives** your **Claude Code** sessions from the
browser, rendered **right-to-left** for comfortable Hebrew / Arabic / Farsi.

It started as a read-only RTL mirror and grew into a full **bidirectional chat**:
read any session, and start new ones you type into — all from the browser.

Everything runs locally. Your conversations never leave your machine.

---

## Features

- **RTL rendering** — right-aligned Hebrew/Arabic; code blocks & tables stay LTR.
- **Multi-session tabs** — one tab per open terminal; switch freely; closed-by-accident
  sessions are recoverable from history.
- **Real open-terminal detection** — "active" means an actually-running `claude`
  process (via live processes + cwd), not a time guess.
- **Bidirectional chat** — `➕ new chat` + a compose box that drives a headless
  `claude -p` session (`--session-id` / `--resume`), reply rendered RTL.
  - **Per-chat folder picker** (which project the chat runs in; default `~`).
  - **Files & images** — attach (📎), paste, or drag-and-drop, with image previews.
  - **Live working indicator** — 🤔 thinking / ✍️ writing / 🔧 tool, elapsed + tokens.
  - **⏹ Stop** — SIGTERM the run (heal-transcript safety net), then continue.
  - **Close (✕)** a chat → moves it to history.
- **Readable tool blocks** — Edit/Write/Bash/Read and plans/questions render
  collapsible & readable (no escaped-`\n` garbage).
- **Footer** — per-session token counter + real Claude Max usage % (via Chrome cookie).
- **Runs forever** — a `launchd` agent keeps it alive across logout/restart.

## How it works

```
~/.claude/projects/**/*.jsonl   ← Claude Code writes sessions here
        │
  extract.py (1s loop)          → sessions.json (index) + s-<id>.md (per session)
  usage.py   (5 min)            → usage.json  (real Max %, via Chrome cookie)
  serve.py   (one process)      → http://127.0.0.1:7778  +  POST /new /send /stop /upload
  index.html                    → the RTL page you read & type in
```

## Requirements
- **macOS**, **Python 3**, **Claude Code** logged in.
- For the Max-% only: **Chrome** logged into claude.ai + `pycryptodome` (auto-installed).

## Install
```bash
git clone https://github.com/<you>/claude-rtl-chat.git
cd claude-rtl-chat
./install.sh        # launchd agent on http://127.0.0.1:7778
```

## Privacy & safety
- 100% local (`127.0.0.1`). Nothing is uploaded.
- Conversations, uploads, usage and personal notes are git-ignored — never committed.
- Browser-driven chats run Claude with a **safe default permission set** (read/plan
  only). Widen it (Edit/Bash) in `serve.py`'s `PERM` at your own discretion.

## License
MIT — see [LICENSE](LICENSE).
