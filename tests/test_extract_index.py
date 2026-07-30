#!/usr/bin/env python3
"""Regression tests for the session index: real-activity time, origin, bg, liveness.

WHY THIS EXISTS (2026-07-30) — three bugs that were all visible in one screenshot of
the tab strip:

  1. STALE JOBS WEARING TODAY'S CLOCK. Three tabs whose last real message was 10-12 July
     sat at the TOP of the strip showing today's time. Claude Code's daemon had re-claimed
     those finished background jobs and appended metadata-only lines (ai-title /
     agent-name — none of them timestamped), which bumps the file's mtime without adding a
     word to the conversation. extract sorted AND displayed by mtime, so a dead job
     outranked a live chat. Fix: `last` = newest real message; mtime stays only as the
     render-cache and history-window signal.

  2. "OPEN IN A TERMINAL" WITH EVERY TERMINAL CLOSED. Liveness compared ps's `comm` field
     to "claude", but macOS prints comm as a full PATH — so it only ever matched argv[0]
     == "claude", which is exactly the daemon's bg-spare / bg-pty-host helpers (they
     outlive the terminal window and keep a ~/.claude/sessions/<pid>.json pointing at the
     job they once hosted), while MISSING real sessions that run as
     .../claude/versions/<v>. Fix: match the command line, exclude the plumbing.

  3. NO ORIGIN. Nothing in the index said where a session came from, so the UI could not
     offer "only what I opened here" vs "only what came from a terminal".

Run:  python3 tests/test_extract_index.py     (or: python3 tests/run.py)
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import extract  # noqa: E402

DAY = 86400


def _iso(epoch):
    """epoch -> the exact shape Claude Code writes: 2026-07-12T14:07:54.713Z"""
    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _msg(role, text, at, entrypoint="cli", bg=False):
    """One real conversation event — timestamped, like every user/assistant line."""
    message = {"role": role, "content": [{"type": "text", "text": text}]}
    if role == "assistant":
        message["usage"] = {"output_tokens": 7}
    ev = {"type": role, "message": message, "timestamp": _iso(at),
          "entrypoint": entrypoint, "userType": "external"}
    if bg:
        ev["sessionKind"] = "bg"
    return ev


def _daemon_touch(sid):
    """The metadata lines a daemon re-claim appends: NO timestamp, no content. These are
    what silently bumped mtime on 18-day-old transcripts."""
    return [{"type": "ai-title", "aiTitle": "job", "sessionId": sid},
            {"type": "agent-name", "agentName": "job", "sessionId": sid},
            {"type": "mode", "mode": "normal", "sessionId": sid}]


class Sandbox:
    """extract with every path global pointed at a temp dir, restored on exit.
    Liveness is stubbed by default so `active` never depends on the real machine."""

    KEYS = ("BASE", "INDEX", "CACHE", "OWNED", "PROJECTS", "SESSIONS_DIR")

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rtl-test-")
        self.projects = os.path.join(self.dir, "projects", "-tmp-proj")
        self.sessions_dir = os.path.join(self.dir, "sessions")
        os.makedirs(self.projects)
        os.makedirs(self.sessions_dir)
        self.saved = {k: getattr(extract, k) for k in self.KEYS}
        self.saved_live = extract.live_session_ids
        extract.BASE = self.dir
        extract.PROJECTS = os.path.dirname(self.projects)
        extract.SESSIONS_DIR = self.sessions_dir
        extract.INDEX = os.path.join(self.dir, "sessions.json")
        extract.CACHE = os.path.join(self.dir, "_cache.json")
        extract.OWNED = os.path.join(self.dir, "owned.json")
        extract.live_session_ids = lambda: set()      # nothing live unless a test says so
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(extract, k, v)
        extract.live_session_ids = self.saved_live
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, sid, events, mtime=None):
        path = os.path.join(self.projects, sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def set_owned(self, d):
        with open(extract.OWNED, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)

    def index(self):
        extract.main()
        with open(extract.INDEX, encoding="utf-8") as fh:
            return {s["id"]: s for s in json.load(fh)["sessions"]}

    def order(self):
        extract.main()
        with open(extract.INDEX, encoding="utf-8") as fh:
            return [s["id"] for s in json.load(fh)["sessions"]]


def test_metadata_touch_is_not_activity():
    """BUG 1, the core of it: a finished job re-claimed today keeps its real time."""
    now = int(time.time())
    ended = now - 18 * DAY                       # last real message: 18 days ago
    with Sandbox() as box:
        box.write("dead-job", [
            _msg("user", "בצע ביקורת", ended - 60),
            _msg("assistant", "סיימתי.", ended),
            *_daemon_touch("dead-job"),           # appended today → mtime = now
        ], mtime=now)
        s = box.index()["dead-job"]

    assert s["last"] == ended, \
        f"last must be the newest REAL message ({ended}), got {s['last']}"
    assert s["mtime"] >= now - 5, "the file really was touched today (test setup sanity)"
    assert s["last"] != s["mtime"], "last must not fall back to the touched mtime"
    assert s["active"] is False, \
        "an 18-day-old conversation must not be 'active' just because mtime is fresh"


def test_index_is_sorted_by_real_activity():
    """BUG 1's user-visible half: the dead job outranked the live chat in the strip."""
    now = int(time.time())
    with Sandbox() as box:
        # the stale job has the NEWEST mtime (touched a second ago) but an ancient
        # conversation; the real chat spoke a minute ago. Sorting by mtime inverts these.
        box.write("stale", [_msg("assistant", "old", now - 18 * DAY),
                            *_daemon_touch("stale")], mtime=now)
        box.write("fresh", [_msg("assistant", "new", now - 60)], mtime=now - 60)
        order = box.order()

    assert order.index("fresh") < order.index("stale"), \
        f"real activity must sort first, got {order}"


def test_origin_classification():
    """BUG 3: web / terminal / mixed, straight from each message's entrypoint."""
    now = int(time.time())
    with Sandbox() as box:
        box.write("from-web", [_msg("user", "היי", now - 30, entrypoint="sdk-cli"),
                               _msg("assistant", "שלום", now - 20, entrypoint="sdk-cli")])
        box.write("from-term", [_msg("user", "hi", now - 30),
                                _msg("assistant", "yo", now - 20)])
        box.write("adopted", [_msg("user", "hi", now - 300),           # born in a terminal
                              _msg("user", "המשך", now - 30, entrypoint="sdk-cli")])
        idx = box.index()

    assert idx["from-web"]["origin"] == "web", idx["from-web"]["origin"]
    assert idx["from-term"]["origin"] == "terminal", idx["from-term"]["origin"]
    assert idx["adopted"]["origin"] == "mixed", \
        "a terminal session we then wrote to from the browser is 'mixed'"


def test_origin_defaults_to_terminal_when_unknown():
    """Old transcripts have no entrypoint field: default to 'not ours'."""
    now = int(time.time())
    with Sandbox() as box:
        ev = _msg("assistant", "legacy", now - 30)
        ev.pop("entrypoint")
        box.write("legacy", [ev])
        assert box.index()["legacy"]["origin"] == "terminal", \
            "unknown origin must not be claimed as a browser chat"


def test_bg_job_is_flagged():
    """A background job the terminal spawned is not a chat you sat in front of."""
    now = int(time.time())
    with Sandbox() as box:
        box.write("job", [_msg("assistant", "working", now - 30, bg=True)])
        box.write("chat", [_msg("assistant", "talking", now - 30)])
        idx = box.index()

    assert idx["job"]["bg"] is True, "sessionKind:bg must surface as bg=True"
    assert idx["chat"]["bg"] is False, "a normal session must not be flagged bg"


def test_liveness_ignores_daemon_plumbing():
    """BUG 2: the daemon's helpers must not count as open conversations, and a real
    session running as the versioned binary must."""
    real = "/Users/x/.local/share/claude/versions/2.1.220 --session-id AAA --resume /p.jsonl"
    fake_ps = {
        "111": real,                                                       # real session
        "222": "claude bg-spare --bg-spare /tmp/cc-daemon/spare/x.claim.sock",
        "333": "claude bg-pty-host --bg-pty-host /tmp/cc-daemon/pty/y.sock 200 50",
        "444": "/Users/x/.local/bin/claude daemon run --origin transient",
        "555": "/opt/homebrew/bin/python3 /Users/x/Projects/claude-rtl-chat/serve.py",
        "666": "/Users/x/.local/bin/claude -p שאלה --resume BBB",           # our own turn
    }
    saved_procs = extract._claude_procs
    with Sandbox() as box:
        extract.live_session_ids = box.saved_live      # exercise the real chain here
        extract._claude_procs = lambda: dict(fake_ps)
        for pid, sid in (("111", "AAA"), ("222", "STALE-JOB"), ("999", "DEAD")):
            with open(os.path.join(box.sessions_dir, pid + ".json"), "w") as fh:
                json.dump({"pid": int(pid), "sessionId": sid, "kind": "bg"}, fh)
        try:
            pids = set(extract._claude_pids())
            live = extract.live_session_ids()
        finally:
            extract._claude_procs = saved_procs

    assert pids == {"111", "666"}, f"only real session processes, got {sorted(pids)}"
    assert live == {"AAA"}, (
        f"expected only the real session; got {sorted(live)} — "
        "STALE-JOB means a bg-spare helper is still counted as an open terminal, "
        "DEAD means a stale pid file is")


def test_owned_chats_stay_open_and_are_web():
    """Browser-owned chats keep working as before: always 'open', always ours — including
    the placeholder for a chat created via /new that has no transcript yet."""
    now = int(time.time())
    with Sandbox() as box:
        box.set_owned({"mine": {"created": now - 10, "title": "שיחה חדשה",
                                "cwd": "/tmp", "perm": "read"},
                       "adopted": {"created": now - 10, "title": "שיחה מאומצת",
                                   "cwd": "/tmp", "perm": "read", "adopted": True}})
        box.write("adopted", [_msg("user", "hi", now - 9 * DAY)])   # ancient, terminal-born
        idx = box.index()

    assert idx["mine"]["active"] is True and idx["mine"]["owned"] is True
    assert idx["mine"]["origin"] == "web", "a chat we created is ours by definition"
    assert idx["mine"]["last"] == now - 10, "placeholder falls back to its creation time"
    assert idx["adopted"]["active"] is True, \
        "an owned chat stays open even with no live process and an old transcript"


def test_cache_reuse_keeps_the_new_fields():
    """Second pass hits the render cache — last/origin/bg must survive it, and a
    pre-upgrade cache entry without them must re-render instead of publishing a
    session with no real timestamp."""
    now = int(time.time())
    with Sandbox() as box:
        box.write("cached", [_msg("user", "היי", now - 40, entrypoint="sdk-cli"),
                             _msg("assistant", "שלום", now - 30, entrypoint="sdk-cli",
                                  bg=True)])
        first = box.index()["cached"]
        second = box.index()["cached"]                     # served from _cache.json
        with open(extract.CACHE, encoding="utf-8") as fh:  # simulate an old cache file
            stale_cache = json.load(fh)
        stale_cache["cached"].pop("last")
        with open(extract.CACHE, "w", encoding="utf-8") as fh:
            json.dump(stale_cache, fh)
        third = box.index()["cached"]

    for name, s in (("first render", first), ("from cache", second),
                    ("legacy cache re-render", third)):
        assert s["last"] == now - 30, f"{name}: last={s['last']}"
        assert s["origin"] == "web", f"{name}: origin={s['origin']}"
        assert s["bg"] is True, f"{name}: bg={s['bg']}"


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import run
    sys.exit(run.main([sys.argv[0], os.path.basename(__file__)[5:-3]]))
