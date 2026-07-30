#!/usr/bin/env python3
"""Regression tests for the send-failure path — save_failure() + parseInterrupted().

WHY THIS EXISTS (2026-07-30): a chat was adopted while a background agent still held its
session. claude refuses that outright —

    Error: Session 0d695686-... is currently running as a background agent (bg).
    Use `claude agents` to find and attach to it, or add --fork-session to branch off a copy.

— and exits 1 before emitting a single token. But run_claude ran it with
stderr=subprocess.DEVNULL, so that sentence went nowhere: the page flashed
"התחיל… ✓ הסתיים", left the unanswered message on screen, and only /tmp/rtl-chat.log knew
anything, as a bare `rc=1`. Six sends in a row, no visible reason. The same silence
swallowed the DNS errors during an internet outage earlier the same day.

The fix has two halves that must agree, and this file guards the seam between them:
  - serve.save_failure() persists the reason, tagged with SEND_FAILED_MARK on line 1
  - index.html's parseInterrupted() keys on that marker to show "שגיאת שליחה" rather than
    claiming Claude's reply was cut off

A stop (⏹) must NOT go down this path — that's a user action, not a failure.

Run:  python3 tests/test_send_failure.py     (or: python3 tests/run.py)
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jsbridge  # noqa: E402
import run  # noqa: E402
import serve  # noqa: E402

SID = "0d695686-0dbb-4d7c-9320-2b3c24ead9f2"

# the real thing, verbatim from the terminal
BG_ERROR = (
    "Error: Session 0d695686-0dbb-4d7c-9320-2b3c24ead9f2 is currently running as a "
    "background agent (bg). Use `claude agents` to find and attach to it, or add "
    "--fork-session to branch off a copy."
)


class Box:
    """serve.BASE pointed at a temp dir, so the .interrupted files land there."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rtl-fail-")
        self.saved = serve.BASE
        serve.BASE = self.dir
        return self

    def __exit__(self, *a):
        serve.BASE = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self, sid=SID):
        with open(serve.interrupted_path(sid), encoding="utf-8") as fh:
            return fh.read()


def _js_parse(raw):
    if not jsbridge.node_available():
        raise run.SkipTest("node is not installed")
    return jsbridge.run(["SEND_FAILED_MARK", "parseInterrupted"],
                        "console.log(JSON.stringify(parseInterrupted(%s)));" % json.dumps(raw))


def test_the_real_error_reaches_the_page():
    """End to end across the seam: what claude printed is what the page will show."""
    with Box() as box:
        serve.save_failure(SID, "", 1, BG_ERROR + "\n")
        raw = box.read()
    got = _js_parse(raw)

    assert got["failed"] == "rc=1", f"the page must report the exit code, got {got['failed']!r}"
    assert "background agent (bg)" in got["text"], \
        "claude's own explanation must survive to the page — that sentence IS the answer"
    assert "claude agents" in got["text"], \
        "keep the actionable hint: it's how the user reaches the running job"


def test_partial_reply_is_kept_alongside_the_error():
    """If tokens did stream before the failure, they're Claude's words — don't drop them."""
    with Box() as box:
        serve.save_failure(SID, "התחלתי לבדוק את", 1, "Error: API timeout")
        raw = box.read()
    got = _js_parse(raw)

    assert got["failed"] == "rc=1"
    assert "התחלתי לבדוק את" in got["text"], "the streamed partial must not be discarded"
    assert "API timeout" in got["text"], "...and neither must the error"


def test_silent_exit_still_explains_itself():
    """claude can die with an empty stderr. An empty error block is worse than useless."""
    with Box() as box:
        serve.save_failure(SID, "", 127, "   \n\n")
        raw = box.read()
    got = _js_parse(raw)

    assert got["failed"] == "rc=127"
    assert got["text"].strip(), "a failure with no stderr must still say something"


def test_stderr_tail_is_bounded_and_keeps_the_end():
    """Verbose stderr must not blow up the page — and the reason is on the LAST lines."""
    noise = "\n".join("warning line %d" % i for i in range(500))
    with Box() as box:
        serve.save_failure(SID, "", 1, noise + "\n" + BG_ERROR)
        raw = box.read()

    assert len(raw) < 3000, f"error block must stay bounded, got {len(raw)} chars"
    assert "background agent (bg)" in raw, "the tail (where the real error is) must survive"
    assert "warning line 0" not in raw, "the head of a 500-line spew must be dropped"


def test_a_user_stop_is_not_a_failure():
    """⏹ sends SIGTERM. That must keep the old quiet behaviour, not raise an error block."""
    assert serve.stopped_by_user(-15) is True, "Popen reports SIGTERM as -15"
    assert serve.stopped_by_user(143) is True, "...or 143 when claude reports it itself"
    assert serve.stopped_by_user(1) is False, "rc=1 is a real failure"
    assert serve.stopped_by_user(127) is False

    with Box() as box:
        serve.save_interrupted(SID, "תשובה חלקית שנקטעה")
        raw = box.read()
    got = _js_parse(raw)

    assert got["failed"] is None, \
        "a stopped turn must render as 'התשובה נקטעה', not as a send failure"
    assert got["text"] == "תשובה חלקית שנקטעה"


class FakeCli:
    """serve wired to a stub `claude` so run_claude() itself can be exercised. Guards the
    wiring the unit tests above can't see: stderr used to go to subprocess.DEVNULL, and no
    amount of save_failure() testing would notice it going back."""

    def __init__(self, script):
        self.script = script

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rtl-cli-")
        self.saved = {k: getattr(serve, k) for k in ("BASE", "OWNED", "PROJECTS", "CLAUDE_BIN")}
        serve.BASE = self.dir
        serve.OWNED = os.path.join(self.dir, "owned.json")
        serve.PROJECTS = os.path.join(self.dir, "projects")      # empty → first turn
        os.makedirs(serve.PROJECTS)
        bin_path = os.path.join(self.dir, "fake-claude")
        with open(bin_path, "w") as fh:
            fh.write(self.script)
        os.chmod(bin_path, 0o755)
        serve.CLAUDE_BIN = bin_path
        with open(serve.OWNED, "w", encoding="utf-8") as fh:
            json.dump({SID: {"cwd": self.dir, "perm": "read", "model": "opus"}}, fh)
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(serve, k, v)
        shutil.rmtree(self.dir, ignore_errors=True)

    def run(self, timeout=25):
        """run_claude in a thread — a stderr deadlock must fail the test, not hang the
        suite for the full 600s wait()."""
        import threading
        t = threading.Thread(target=serve.run_claude, args=(SID, "בדיקה"), daemon=True)
        t.start()
        t.join(timeout)
        assert not t.is_alive(), (
            f"run_claude still running after {timeout}s — the child is blocked writing "
            "stderr (that's why it goes to a temp file, not an undrained pipe)")
        try:
            with open(serve.interrupted_path(SID), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""


def test_run_claude_captures_the_childs_stderr():
    """The regression that started it all: a failing turn must leave the reason behind."""
    raw = FakeCli('#!/bin/sh\necho "Error: boom from the CLI" >&2\nexit 1\n').__enter__()
    try:
        out = raw.run()
    finally:
        raw.__exit__()

    assert out.startswith(serve.SEND_FAILED_MARK), \
        f"no failure block was saved — stderr went to /dev/null again? got {out[:80]!r}"
    assert "boom from the CLI" in out, "the child's stderr must reach the file"


def test_a_flood_of_stderr_does_not_deadlock_the_turn():
    """~256KB of stderr while we're busy draining stdout. An undrained PIPE would fill its
    buffer and block the child forever; a temp file can't."""
    script = ('#!/bin/sh\n'
              'i=0\n'
              'while [ $i -lt 4000 ]; do\n'
              '  echo "noisy stderr line $i ........................................" >&2\n'
              '  i=$((i+1))\n'
              'done\n'
              'echo "Error: the real reason, last line" >&2\n'
              'exit 1\n')
    box = FakeCli(script).__enter__()
    try:
        out = box.run()
    finally:
        box.__exit__()

    assert out.startswith(serve.SEND_FAILED_MARK), "the turn must still report its failure"
    assert "the real reason, last line" in out, "the tail is what matters — keep it"
    assert len(out) < 3000, f"and it must stay bounded, got {len(out)} chars"


def test_a_clean_turn_leaves_no_failure_block():
    """Success must not write anything: a stale block would haunt the next reply."""
    box = FakeCli('#!/bin/sh\necho "{}"\nexit 0\n').__enter__()
    try:
        out = box.run()
    finally:
        box.__exit__()
    assert out == "", f"a successful turn must leave no interrupted file, got {out[:80]!r}"


def test_both_sides_use_the_same_marker():
    """The marker is a string shared by serve.py and index.html. If one side is edited
    alone, failures silently start rendering as 'Claude's reply was cut off' again."""
    js_mark = jsbridge.extract(["SEND_FAILED_MARK"])
    assert serve.SEND_FAILED_MARK in js_mark, (
        f"index.html's SEND_FAILED_MARK does not match serve.py's "
        f"({serve.SEND_FAILED_MARK!r}): {js_mark.strip()}")


def test_text_that_merely_mentions_the_marker_is_not_a_failure():
    """Only line 1 counts — a reply that happens to discuss the marker stays a reply."""
    got = _js_parse("Claude explaining the protocol:\n" + serve.SEND_FAILED_MARK + " rc=1")
    assert got["failed"] is None, "the marker only counts as the FIRST line"


if __name__ == "__main__":
    sys.exit(run.main([sys.argv[0], os.path.basename(__file__)[5:-3]]))
