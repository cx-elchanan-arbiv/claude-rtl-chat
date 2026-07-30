#!/usr/bin/env python3
"""Regression tests for tableToMd() — the "copy a table as a table" fix.

THE BUG (2026-07-30): both copy buttons (📋 העתק הכל on a reply, 📋 העתק in the render
pad) read text off the rendered DOM. A <table> read as text is tab-separated soup, so
the moment boxToMd() started producing real tables, everything you copied out of the
page lost its table structure on paste.

THE FIX: copyRich() re-serialises every <table> back to markdown pipe rows for the
plain-text clipboard flavour, and puts the rendered HTML on the clipboard alongside it
(text/html) so Gmail / Docs / Word / Slack paste a real formatted table.

tableToMd() is the pure part and is what's tested here. It reads only .rows / .cells /
.tHead / .textContent, so a plain object with that shape exercises the real logic
without needing a DOM.

NOT covered here: copyRich()'s off-screen-clone step and the ClipboardItem write, both
of which need a real browser. If you touch those, verify by hand — copy a reply with a
table into a plain-text editor (expect markdown pipe rows, and blank lines between
paragraphs) and into Gmail (expect a formatted table).

Run:  python3 tests/test_rich_copy.py     (or: python3 tests/run.py)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jsbridge  # noqa: E402
import run  # noqa: E402

SYMBOLS = ["tableToMd"]

# A <table> as tableToMd() sees it. `head` rows go into tHead (what marked emits for a
# markdown table); pass head=0 to model a table with no <thead>.
FAKE_TABLE = """
function fakeTable(rows, headCount) {
  const mk = r => ({ cells: r.map(t => ({ textContent: t })) });
  const all = rows.map(mk);
  return {
    rows: all,
    tHead: headCount > 0 ? { rows: all.slice(0, headCount) } : null,
  };
}
"""


def table_to_md(rows, head_count=1):
    if not jsbridge.node_available():
        raise run.SkipTest("node is not installed")
    script = FAKE_TABLE + (
        "console.log(JSON.stringify(tableToMd(fakeTable(%s, %d))));"
        % (json.dumps(rows), head_count))
    return jsbridge.run(SYMBOLS, script)


def test_header_and_body_are_separated_by_the_delimiter_row():
    out = table_to_md([["מה", "עלות"], ["מודל small", "חינם"]], head_count=1)
    lines = out.splitlines()
    assert lines[0] == "| מה | עלות |", f"header wrong: {lines[0]!r}"
    assert lines[1] == "| --- | --- |", f"delimiter row wrong: {lines[1]!r}"
    assert lines[2] == "| מודל small | חינם |", f"body wrong: {lines[2]!r}"


def test_table_without_thead_treats_the_first_row_as_the_header():
    # markdown has no concept of a headerless table; emitting a body-only table
    # produces output that doesn't render as a table at all on paste
    out = table_to_md([["א", "ב"], ["ג", "ד"]], head_count=0)
    lines = out.splitlines()
    assert lines[1] == "| --- | --- |", \
        f"headerless table must still get a delimiter after row 1, got {lines!r}"


def test_pipe_inside_a_cell_is_escaped():
    # an unescaped | splits the cell and shifts every column after it
    out = table_to_md([["a | b", "ג"]], head_count=1)
    assert r"a \| b" in out, "a literal | in a cell must be escaped on copy"


def test_newlines_in_a_cell_are_collapsed():
    # a wrapped cell contains newlines; leaving them in breaks the row into pieces
    # and the pasted markdown stops being a table
    out = table_to_md([["שורה\nראשונה", "ב"]], head_count=1)
    assert "\n" not in out.splitlines()[0], "cell newline leaked into the row"
    assert "שורה ראשונה" in out, "cell text was mangled while collapsing whitespace"


def test_ragged_rows_are_padded():
    # every row must carry the same cell count as the delimiter, or the pasted
    # table silently loses columns
    out = table_to_md([["א", "ב", "ג"], ["ד"]], head_count=1)
    for line in out.splitlines():
        assert line.count("|") == 4, f"row has wrong cell count: {line!r}"


def test_empty_table_returns_empty_string():
    # copyRich() substitutes the result into the text flavour; an exception here
    # would break the whole copy, so a degenerate table must return cleanly
    assert table_to_md([], head_count=0) == "", "an empty table must serialise to ''"


if __name__ == "__main__":
    sys.exit(run.main([sys.argv[0], os.path.basename(__file__)[5:-3]]))
