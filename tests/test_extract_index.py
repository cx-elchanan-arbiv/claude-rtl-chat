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

    KEYS = ("BASE", "INDEX", "CACHE", "OWNED", "HIDDEN", "PROJECTS", "SESSIONS_DIR")

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rtl-test-")
        self.projects = os.path.join(self.dir, "projects", "-tmp-proj")
        self.sessions_dir = os.path.join(self.dir, "sessions")
        os.makedirs(self.projects)
        os.makedirs(self.sessions_dir)
        self.saved = {k: getattr(extract, k) for k in self.KEYS}
        self.saved_live = extract.live_session_ids
        self.saved_counts = extract.live_counts
        extract.BASE = self.dir
        extract.PROJECTS = os.path.dirname(self.projects)
        extract.SESSIONS_DIR = self.sessions_dir
        extract.INDEX = os.path.join(self.dir, "sessions.json")
        extract.CACHE = os.path.join(self.dir, "_cache.json")
        extract.OWNED = os.path.join(self.dir, "owned.json")
        extract.HIDDEN = os.path.join(self.dir, "hidden.json")
        extract.live_session_ids = lambda: set()      # nothing live unless a test says so
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(extract, k, v)
        extract.live_session_ids = self.saved_live
        extract.live_counts = self.saved_counts
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

    def set_hidden(self, ids):
        with open(extract.HIDDEN, "w", encoding="utf-8") as fh:
            json.dump(ids, fh, ensure_ascii=False)

    def index(self):
        extract.main()
        with open(extract.INDEX, encoding="utf-8") as fh:
            return {s["id"]: s for s in json.load(fh)["sessions"]}

    def raw_index(self):
        extract.main()
        with open(extract.INDEX, encoding="utf-8") as fh:
            return json.load(fh)

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


def test_live_bg_ids_finds_only_running_background_agents():
    """/adopt uses this to refuse a session a background agent is holding — `claude -p
    --resume` rejects those outright, so adopting would hand over a chat box whose every
    send fails (which is exactly what happened: six silent rc=1 sends)."""
    versioned = "/Users/x/.local/share/claude/versions/2.1.220 --session-id %s"
    fake_ps = {"111": versioned % "JOB", "222": versioned % "CHAT",
               "333": "claude bg-spare --bg-spare /tmp/cc-daemon/spare/x.claim.sock"}
    saved_procs = extract._claude_procs
    with Sandbox() as box:
        extract._claude_procs = lambda: dict(fake_ps)
        for pid, sid, kind in (("111", "JOB", "bg"), ("222", "CHAT", "interactive"),
                               ("333", "STALE-JOB", "bg"), ("999", "DEAD-JOB", "bg")):
            with open(os.path.join(box.sessions_dir, pid + ".json"), "w") as fh:
                json.dump({"pid": int(pid), "sessionId": sid, "kind": kind}, fh)
        try:
            bg = extract.live_bg_ids()
            extract._claude_procs = lambda: None          # ps unusable → liveness unknown
            unknown = extract.live_bg_ids()
        finally:
            extract._claude_procs = saved_procs

    assert bg == {"JOB"}, (
        f"expected only the running bg agent, got {sorted(bg)} — CHAT means an interactive "
        "session is being blocked, STALE-JOB/DEAD-JOB mean a finished job still blocks adoption")
    assert unknown == set(), "unknown liveness must fail OPEN — never block on a guess"


def test_a_guessed_open_is_labelled_as_a_guess():
    """When we can't observe which sessions a process holds, extract falls back to
    guessing — and the index must SAY so, per session and for the run as a whole.
    Presenting that guess as fact is exactly how finished background jobs sat in the open
    row for weeks looking like live conversations."""
    now = int(time.time())
    with Sandbox() as box:
        box.write("recent", [_msg("assistant", "דיבר עכשיו", now - 60)])
        box.write("old", [_msg("assistant", "מזמן", now - 5 * DAY)])

        extract.live_session_ids = lambda: None           # no process signal at all
        extract.live_counts = lambda: {}
        idx = box.raw_index()
        by_id = {s["id"]: s for s in idx["sessions"]}

    assert idx["liveness"] == "time", f"the run must declare how it decided, got {idx.get('liveness')}"
    assert by_id["recent"]["active"] is True, "the time window still marks it open..."
    assert by_id["recent"]["guess"] is True, "...but it must be flagged as a guess"
    assert by_id["old"]["guess"] is False, "a session we did NOT claim is open isn't a guess"


def test_an_observed_open_is_not_labelled_a_guess():
    """The normal path: we know exactly which sessions are held, so nothing is hedged."""
    now = int(time.time())
    with Sandbox() as box:
        box.write("held", [_msg("assistant", "טקסט", now - 60)])
        extract.live_session_ids = lambda: {"held"}
        idx = box.raw_index()

    assert idx["liveness"] == "exact", "an observed run must not be marked as guessing"
    assert idx["sessions"][0]["active"] is True
    assert idx["sessions"][0]["guess"] is False, \
        "a session we actually observed must never be marked ≈ — that would cry wolf"


def test_an_owned_chat_is_never_a_guess():
    """Our own chats are open because WE hold them; no process detection involved."""
    now = int(time.time())
    with Sandbox() as box:
        box.set_owned({"mine": {"created": now - 10, "title": "שיחה", "cwd": "/tmp",
                                "perm": "read"}})
        box.write("mine", [_msg("user", "היי", now - 5 * DAY, entrypoint="sdk-cli")])
        extract.live_session_ids = lambda: None            # even with no signal at all
        extract.live_counts = lambda: {}
        idx = box.raw_index()

    s = {x["id"]: x for x in idx["sessions"]}["mine"]
    assert s["active"] is True and s["guess"] is False, \
        "an owned chat is open by ownership, not by guesswork"


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


def test_hidden_sessions_leave_the_index_but_not_the_disk():
    """✕ on a history tab hides it from the strip. It must be a VIEW list only: the
    transcript in ~/.claude/projects is Claude Code's data — the terminal can still resume
    that session — so hiding may never touch it. Our own rendered copy DOES go, which is
    the point: those are the megabytes we can reclaim."""
    now = int(time.time())
    with Sandbox() as box:
        path = box.write("boring", [_msg("assistant", "מילה שלא רוצה לראות", now - 3 * DAY)])
        box.write("keeper", [_msg("assistant", "כן", now - 3 * DAY)])
        first = box.index()
        assert "boring" in first, "test setup: it must be there before hiding"
        md = os.path.join(box.dir, "s-boring.md")
        assert os.path.exists(md), "test setup: its rendered copy exists before hiding"

        box.set_hidden(["boring"])
        idx = box.raw_index()
        # both checked INSIDE the sandbox — its temp dir is gone once the block exits
        transcript_survived = os.path.exists(path)
        rendered_copy_left = os.path.exists(md)

    ids = {s["id"] for s in idx["sessions"]}
    assert "boring" not in ids, "a hidden session must not reach the page"
    assert "keeper" in ids, "and hiding one must not disturb the others"
    assert idx["hidden"] == 1, f"the page needs the count for its restore button, got {idx}"
    assert transcript_survived, \
        "THE TRANSCRIPT MUST SURVIVE — it belongs to Claude Code, not to us"
    assert not rendered_copy_left, "our own rendered copy should be reclaimed"


def test_unhiding_brings_it_straight_back():
    """The way out of a ✕ — otherwise hiding is a one-way door."""
    now = int(time.time())
    with Sandbox() as box:
        box.write("boring", [_msg("assistant", "טקסט", now - 3 * DAY)])
        box.set_hidden(["boring"])
        assert "boring" not in box.index()
        box.set_hidden([])                       # what /unhide-all writes
        back = box.index()

    assert "boring" in back, "restoring must re-render it from the transcript"
    assert back["boring"]["last"] == now - 3 * DAY, "...with its real time intact"


def test_a_hidden_session_reappears_if_it_goes_live_again():
    """Hiding means 'get this out of my history', not 'blacklist forever'. If a terminal
    picks the session up again it's live work and must be visible."""
    now = int(time.time())
    with Sandbox() as box:
        box.write("revived", [_msg("assistant", "טקסט", now - 3 * DAY)])
        box.set_hidden(["revived"])
        assert "revived" not in box.index(), "hidden while nothing holds it"
        extract.live_session_ids = lambda: {"revived"}          # a terminal resumed it
        idx = box.raw_index()

    assert "revived" in idx["sessions"][0]["id"], "a live session must override hiding"
    assert idx["hidden"] == 0, "and it must not be counted as hidden while it's live"


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
