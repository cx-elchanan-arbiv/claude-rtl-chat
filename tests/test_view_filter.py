#!/usr/bin/env python3
"""Regression tests for the tab-strip view filter — actAt / isWeb / inView / originNote.

WHY THIS EXISTS (2026-07-30): the strip mixed three unrelated things with no way to tell
them apart — chats opened in this UI, terminal sessions we only mirror, and background
jobs the terminal spawned (which the Claude Code daemon keeps alive long after the
terminal window is gone). The view picker splits them, and these four helpers are the
whole rule set behind it:

  - actAt()      when the chat last actually SAID something. Never the file's mtime: a
                 daemon re-claim appends untimestamped metadata lines, which is how
                 conversations from 10-12 July ended up displaying today's time.
  - isWeb()      "is this ours" — owned OR web-origin OR mixed (adopted then written to).
                 An adopted chat must count as ours: you can type into it.
  - inView()     the mode filter itself. 'all' must never hide anything.
  - originNote() the tooltip that answers "what even is this tab".

These are pure functions pulled live out of index.html by name, so a rename fails here
loudly instead of silently turning the filter into a no-op.

Run:  python3 tests/test_view_filter.py     (or: python3 tests/run.py)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jsbridge  # noqa: E402
import run  # noqa: E402

SYMBOLS = ["actAt", "isWeb", "inView", "originNote"]

# the four kinds of tab that share the strip
WEB = {"id": "w", "owned": True, "origin": "web", "bg": False}
ADOPTED = {"id": "a", "owned": True, "origin": "mixed", "bg": False}
TERMINAL = {"id": "t", "owned": False, "origin": "terminal", "bg": False}
JOB = {"id": "j", "owned": False, "origin": "terminal", "bg": True}


def _eval(script):
    if not jsbridge.node_available():
        raise run.SkipTest("node is not installed")
    return jsbridge.run(SYMBOLS, "let viewMode = 'all';\n" + script)


def _call(fn, session):
    return _eval("console.log(JSON.stringify(%s(%s)));" % (fn, json.dumps(session)))


def test_actAt_prefers_the_real_message_time():
    # the exact production shape: file touched now, last real message 18 days ago
    touched = {"last": 1000, "mtime": 999999}
    assert _call("actAt", touched) == 1000, \
        "actAt must use `last`; using mtime is what floated dead jobs to the top"


def test_actAt_falls_back_to_mtime_for_a_pre_upgrade_index():
    # sessions.json written by an older serve.py has no `last` — must not render NaN/Invalid
    assert _call("actAt", {"mtime": 4242}) == 4242, \
        "a session index without `last` must still show a time"


def test_isWeb_covers_owned_web_and_adopted():
    assert _call("isWeb", WEB) is True
    assert _call("isWeb", ADOPTED) is True, \
        "an adopted chat is ours — you can write to it, so it belongs to the web view"
    assert _call("isWeb", TERMINAL) is False
    assert _call("isWeb", JOB) is False


def test_isWeb_trusts_owned_even_before_the_first_web_message():
    # /adopt marks it owned immediately; origin only flips to mixed after we send a turn
    just_adopted = {"id": "x", "owned": True, "origin": "terminal", "bg": False}
    assert _call("isWeb", just_adopted) is True, \
        "a chat we own must never be filtered into the terminal-only view"


def test_view_all_hides_nothing():
    got = _eval("""
      viewMode = 'all';
      console.log(JSON.stringify([%s, %s, %s, %s].map(inView)));
    """ % (json.dumps(WEB), json.dumps(ADOPTED), json.dumps(TERMINAL), json.dumps(JOB)))
    assert got == [True, True, True, True], f"'all' must show everything, got {got}"


def test_view_web_shows_only_ours():
    got = _eval("""
      viewMode = 'web';
      console.log(JSON.stringify([%s, %s, %s, %s].map(inView)));
    """ % (json.dumps(WEB), json.dumps(ADOPTED), json.dumps(TERMINAL), json.dumps(JOB)))
    assert got == [True, True, False, False], f"web view leaked terminal chats: {got}"


def test_view_terminal_shows_only_mirrored():
    got = _eval("""
      viewMode = 'terminal';
      console.log(JSON.stringify([%s, %s, %s, %s].map(inView)));
    """ % (json.dumps(WEB), json.dumps(ADOPTED), json.dumps(TERMINAL), json.dumps(JOB)))
    assert got == [False, False, True, True], f"terminal view leaked our own chats: {got}"


def test_the_two_filtered_views_are_complementary():
    """No chat may fall through both filters — that's how a tab disappears for good."""
    for s in (WEB, ADOPTED, TERMINAL, JOB):
        pair = _eval("""
          const s = %s;
          viewMode = 'web'; const a = inView(s);
          viewMode = 'terminal'; const b = inView(s);
          console.log(JSON.stringify([a, b]));
        """ % json.dumps(s))
        assert pair.count(True) == 1, \
            f"{s['id']}: appears in {pair.count(True)} of the two views, must be exactly 1"


def test_originNote_explains_a_background_job():
    assert "job" in _call("originNote", JOB), \
        "a bg job's tooltip must say so — it's the tab nobody can identify"
    assert _call("originNote", TERMINAL) == "נפתח בטרמינל"
    assert _call("originNote", WEB) == "נפתח בדפדפן"
    assert "אומץ" in _call("originNote", ADOPTED)


if __name__ == "__main__":
    sys.exit(run.main([sys.argv[0], os.path.basename(__file__)[5:-3]]))
