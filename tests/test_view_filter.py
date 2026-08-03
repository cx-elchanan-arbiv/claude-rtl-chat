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

SYMBOLS = ["VIEW_MODES", "JOB_MODES", "normalizeViewMode", "normalizeJobMode",
           "actAt", "isWeb", "inSource", "inKind", "inView", "originNote", "histOn",
           "mostRecent", "neighbourOf", "pinLabel", "pickAfterFilter", "livenessNote"]

# the four kinds of tab that share the strip
WEB = {"id": "w", "owned": True, "origin": "web", "bg": False}
ADOPTED = {"id": "a", "owned": True, "origin": "mixed", "bg": False}
TERMINAL = {"id": "t", "owned": False, "origin": "terminal", "bg": False}
JOB = {"id": "j", "owned": False, "origin": "terminal", "bg": True}
WEB_JOB = {"id": "wj", "owned": True, "origin": "mixed", "bg": True}   # adopted bg job
ALL_FOUR = [WEB, ADOPTED, TERMINAL, JOB]


def _eval(script):
    if not jsbridge.node_available():
        raise run.SkipTest("node is not installed")
    return jsbridge.run(
        SYMBOLS, "let viewMode = 'both', jobMode = 'both', showHistory = false;\n" + script)


def _in_view(sessions, view="both", job="both"):
    """Which of `sessions` survive a given (source, kind) filter combination."""
    return _eval("viewMode = %s; jobMode = %s;\nconsole.log(JSON.stringify(%s.map(inView)));"
                 % (json.dumps(view), json.dumps(job), json.dumps(sessions)))


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


def test_both_hides_nothing():
    """The default. Renamed from 'all' — the label now says WHAT it shows
    ("דפדפן + טרמינל"), because "הכל" told you nothing about what was in the strip."""
    got = _in_view(ALL_FOUR)
    assert got == [True, True, True, True], f"the default must show everything, got {got}"


def test_source_web_shows_only_ours():
    got = _in_view(ALL_FOUR, view="web")
    assert got == [True, True, False, False], f"web view leaked terminal chats: {got}"


def test_source_terminal_shows_only_mirrored():
    got = _in_view(ALL_FOUR, view="terminal")
    assert got == [False, False, True, True], f"terminal view leaked our own chats: {got}"


def test_the_two_source_views_are_complementary():
    """No chat may fall through both filters — that's how a tab disappears for good."""
    for s in ALL_FOUR:
        web, term = _in_view([s], view="web")[0], _in_view([s], view="terminal")[0]
        assert [web, term].count(True) == 1, \
            f"{s['id']}: appears in {[web, term].count(True)} of the two source views, must be 1"


def test_kind_filter_splits_chats_from_jobs():
    """The second selector, on its own axis: conversations you talked in vs. background
    agents the terminal spawned."""
    assert _in_view(ALL_FOUR, job="chats") == [True, True, True, False], "a job leaked into 'chats'"
    assert _in_view(ALL_FOUR, job="jobs") == [False, False, False, True], "'jobs' must show only jobs"
    for s in ALL_FOUR:
        pair = [_in_view([s], job="chats")[0], _in_view([s], job="jobs")[0]]
        assert pair.count(True) == 1, f"{s['id']}: must be in exactly one of chats/jobs, got {pair}"


def test_the_filters_are_independent():
    """The whole point of two selectors instead of one list: every combination is reachable.
    An adopted bg job is the case that proves the axes really are independent — it's ours
    AND a job, so it must survive web+jobs and vanish from web+chats."""
    grid = {(v, j): _in_view([WEB_JOB], view=v, job=j)[0]
            for v in ("both", "web", "terminal") for j in ("both", "chats", "jobs")}
    assert grid[("web", "jobs")] is True, "an adopted job must show under דפדפן + ג'ובים"
    assert grid[("web", "chats")] is False, "...and must not show under דפדפן + שיחות"
    assert grid[("terminal", "jobs")] is False, "it's ours, not the terminal's"
    assert grid[("both", "both")] is True, "and it must be visible with no filtering at all"

    # a terminal job: the mirror image
    assert _in_view([JOB], view="terminal", job="jobs")[0] is True
    assert _in_view([JOB], view="web", job="jobs")[0] is False


def test_every_session_is_reachable_by_some_combination():
    """A session that no combination can show would be invisible forever."""
    for s in ALL_FOUR + [WEB_JOB]:
        shown = [(v, j) for v in ("both", "web", "terminal") for j in ("both", "chats", "jobs")
                 if _in_view([s], view=v, job=j)[0]]
        assert shown, f"{s['id']} is unreachable in every filter combination"


def test_history_mode_needs_history_to_engage():
    """The trap this guards (caught in the browser, 2026-07-30): switch the view to
    מהדפדפן and the filtered history is empty — every history entry is terminal-born. The
    strip then rendered that empty history while the היסטוריה button hid itself for being
    empty, so there were no tabs at all and no way back except changing the filter."""
    got = _eval("""
      showHistory = true;
      const withHistory = histOn([{id: 'h'}]);
      const noHistory = histOn([]);
      showHistory = false;
      const off = histOn([{id: 'h'}]);
      console.log(JSON.stringify([withHistory, noHistory, off]));
    """)
    assert got == [True, False, False], \
        f"history mode must need BOTH the toggle and a non-empty history, got {got}"


def test_landing_after_a_tab_disappears_picks_the_newest():
    """THE BUG (2026-07-30, reported with screenshots): hiding a history tab dropped the
    user on a 0-turn session from 06:45 that rendered as an empty "ממתין לתשובה הראשונה…"
    screen — and then followed them into the open view as a pinned tab. The fallback used
    pool[0], i.e. the first entry of the deliberately-stable tab order, which has nothing
    to do with recency and had that junk session at the front."""
    junk = {"id": "junk", "last": 1000, "turns": 0}          # what it used to choose
    real = {"id": "real", "last": 9000, "turns": 12}
    older = {"id": "older", "last": 5000, "turns": 4}
    got = _eval("console.log(JSON.stringify(mostRecent(%s)));"
                % json.dumps([junk, real, older]))           # junk deliberately first
    assert got["id"] == "real", \
        f"must land on the newest real activity, not the head of the tab order (got {got['id']})"

    assert _eval("console.log(JSON.stringify(mostRecent([])));") is None, \
        "an empty pool must yield nothing to select, not a crash"
    assert _eval("console.log(JSON.stringify(mostRecent(%s)));" % json.dumps([junk]))["id"] == "junk", \
        "one candidate is still the answer, empty or not"


def test_closing_a_tab_hands_over_to_its_neighbour():
    """Like a browser: the tab beside the one you closed, never a jump across the strip."""
    strip = ["a", "b", "c"]
    nb = lambda ids, i: _eval("console.log(JSON.stringify(neighbourOf(%s, %s)));"
                              % (json.dumps(ids), json.dumps(i)))
    assert nb(strip, "b") == "c", "middle tab → the next one"
    assert nb(strip, "c") == "b", "last tab → the previous one"
    assert nb(strip, "a") == "b", "first tab → the next one"
    assert nb(["only"], "only") is None, "closing the last tab leaves nothing selected"
    assert nb(strip, "missing") is None, "an id that isn't in the strip has no neighbour"


def test_a_saved_mode_from_the_old_naming_still_works():
    """The source filter's first option was called 'all' before it was renamed to say what
    it shows ("דפדפן + טרמינל"). A browser that still has 'all' in localStorage must land on
    the equivalent mode — an unrecognised value would make every predicate fall through and
    show an empty strip."""
    assert _eval("console.log(JSON.stringify(normalizeViewMode('all')));") == "both", \
        "the legacy 'all' must migrate to 'both', not become an unknown mode"
    assert _eval("console.log(JSON.stringify(normalizeViewMode('web')));") == "web"
    assert _eval("console.log(JSON.stringify(normalizeViewMode(null)));") == "both"
    assert _eval("console.log(JSON.stringify(normalizeViewMode('nonsense')));") == "both", \
        "any junk value must fall back to showing everything, never to showing nothing"
    assert _eval("console.log(JSON.stringify(normalizeJobMode('jobs')));") == "jobs"
    assert _eval("console.log(JSON.stringify(normalizeJobMode('')));") == "both"


def test_a_kept_tab_never_looks_like_it_belongs():
    """THE CONFUSION (2026-07-30, reported twice with screenshots): a closed conversation sat
    in the row of OPEN chats and read as open — it was only there because it was the one
    being read, but nothing said so. Every "kept" state must announce itself."""
    def pin(is_hist, is_pinned, kept):
        return _eval("console.log(JSON.stringify(pinLabel(%s, %s, %s)));"
                     % (json.dumps(is_hist), json.dumps(is_pinned), json.dumps(kept)))

    normal = pin(False, False, False)
    assert normal["prefix"] == "" and normal["note"] == "", \
        "a tab that belongs in the strip must carry no marker at all"

    closed_in_open_row = pin(True, True, False)
    assert "היסטוריה" in closed_in_open_row["prefix"], \
        "a closed chat held in the open row MUST say היסטוריה — this is the reported bug"
    assert "רק כי אתה קורא אותה" in closed_in_open_row["note"]

    open_in_history_row = pin(False, True, False)
    assert "פתוח" in open_in_history_row["prefix"], "and the mirror case must say פתוח"

    filtered_out = pin(False, False, True)
    assert "מחוץ לסינון" in filtered_out["note"], \
        "a tab the filter excludes, kept only because you're reading it, must say so"


def test_switching_filters_lands_you_somewhere_visible():
    """Caught in the browser: the candidate search used the already-FILTERED list, so
    switching to a mode that list had nothing for left the user parked on a chat the new
    filter hides (a job showing under 'שיחות')."""
    all_sessions = [JOB, TERMINAL, WEB]

    def pick(current, view="both", job="both"):
        return _eval("viewMode = %s; jobMode = %s;\nconsole.log(JSON.stringify(pickAfterFilter(%s, %s)));"
                     % (json.dumps(view), json.dumps(job), json.dumps(all_sessions), json.dumps(current)))

    assert pick("j", job="chats") == "t", \
        "on a job and switching to 'שיחות' → must move to a real chat, got the job again"
    assert pick("t", job="chats") == "t", "already visible → must not be moved at all"
    assert pick("t", view="web") == "w", "a terminal chat under 'דפדפן' → move to a web chat"
    assert pick("nosuch") == "nosuch", "an unknown selection is left alone (a pending new chat)"
    # nothing matches → stay put rather than blanking the screen
    assert _eval("viewMode = 'web'; jobMode = 'jobs';\nconsole.log(JSON.stringify(pickAfterFilter(%s, 't')));"
                 % json.dumps([TERMINAL])) == "t", \
        "when no session matches the new filter, keep showing what you had"


def test_the_page_admits_when_open_is_only_a_guess():
    """extract has two fallbacks for "what's open" when it can't observe processes, and
    both are guesses. The status line must say so — a guess presented as fact is what let
    finished background jobs sit in the open row looking live."""
    def note(kind):
        return _eval("console.log(JSON.stringify(livenessNote(%s)));" % json.dumps(kind))

    exact = note("exact")
    assert exact["suffix"] == "", "an observed run must not hedge — that would cry wolf"
    assert exact["title"], "but it should still explain how it knows, on hover"

    for guessy in ("project", "time"):
        n = note(guessy)
        assert "משוער" in n["suffix"], f"'{guessy}' is a guess and must be labelled as one"
        assert "ניחוש" in n["title"], f"'{guessy}' must explain WHY it's uncertain"

    assert note("project")["title"] != note("time")["title"], \
        "the two fallbacks fail differently — the explanation should say which one it is"
    assert note(undefined_kind := None)["suffix"] == "", \
        "an index from an older serve.py (no liveness field) must not claim uncertainty"


def test_originNote_explains_a_background_job():
    assert "job" in _call("originNote", JOB), \
        "a bg job's tooltip must say so — it's the tab nobody can identify"
    assert _call("originNote", TERMINAL) == "נפתח בטרמינל"
    assert _call("originNote", WEB) == "נפתח בדפדפן"
    assert "אומץ" in _call("originNote", ADOPTED)


if __name__ == "__main__":
    sys.exit(run.main([sys.argv[0], os.path.basename(__file__)[5:-3]]))
