#!/usr/bin/env python3
"""Regression tests for boxToMd() — the box-drawing-table fix.

THE BUG (2026-07-30): Claude Code draws tables in the terminal with ┌─┬┐ │ ├─┼┤ └─┴┘.
Pasted into this RTL page they are just paragraph text as far as marked is concerned,
so the bidi algorithm reorders every cell and the table collapses into unreadable
noise — cells, borders and columns scattered across the line.

THE FIX: boxToMd() runs ahead of every marked.parse() and rebuilds those blocks as
markdown pipe tables, so marked emits a real <table> that RTL lays out correctly.

The delicate half of this fix is what it must NOT touch. Every "untouched" test below
is a real way a naive implementation breaks the page:
  - a normal markdown table would be re-parsed and its |:---:| alignment row mangled
  - a box table inside a ``` fence would stop being a code sample
  - prose that merely mentions a box character would get mutilated mid-sentence
  - extract.py's one-line tool <details> is raw HTML — rewriting it corrupts the DOM

Run:  python3 tests/test_box_tables.py     (or: python3 tests/run.py)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jsbridge  # noqa: E402
import run  # noqa: E402

# Everything boxToMd() needs, pulled live out of index.html.
SYMBOLS = ["BOX_ANY", "BOX_START", "BOX_VERT", "isRule", "isRow",
           "boxBlockToMd", "boxToMd"]


def box_to_md(text):
    if not jsbridge.node_available():
        raise run.SkipTest("node is not installed")
    return jsbridge.run(
        SYMBOLS, "console.log(JSON.stringify(boxToMd(%s)));" % json.dumps(text))


def rows_of(md):
    """The `| a | b |` lines of the rendered output, minus the |---| separator."""
    return [ln.strip() for ln in md.splitlines()
            if ln.strip().startswith("|") and "---" not in ln]


# --- the conversion itself ---------------------------------------------------

def test_box_table_becomes_a_markdown_table():
    # the exact shape from the 2026-07-30 screenshot: 5 columns, Hebrew + latin mixed
    src = """  שלב 1 — חלוקה נכונה לכתוביות

  ┌────────────────────┬──────────────────────────┬────────┐
  │         מה         │          יתרון           │ חיסרון │
  ├────────────────────┼──────────────────────────┼────────┤
  │ מילים במקום מקטעים │ שליטה מלאה על הגבולות    │ —      │
  ├────────────────────┼──────────────────────────┼────────┤
  │ מיזוג שברים        │ אין יותר הבזקים          │ —      │
  └────────────────────┴──────────────────────────┴────────┘
"""
    out = box_to_md(src)
    rows = rows_of(out)

    assert "┌" not in out and "│" not in out, \
        "box-drawing characters survived — the block was not converted"
    assert "| --- | --- | --- |" in out, "missing the markdown delimiter row"
    assert rows[0] == "| מה | יתרון | חיסרון |", f"header row wrong: {rows[0]!r}"
    assert len(rows) == 3, f"expected header + 2 body rows, got {len(rows)}"
    assert "| מילים במקום מקטעים | שליטה מלאה על הגבולות | — |" == rows[1], \
        f"first body row wrong: {rows[1]!r}"
    # surrounding prose must survive untouched
    assert "שלב 1 — חלוקה נכונה לכתוביות" in out, "text above the table was eaten"


def test_cell_wrapped_over_two_lines_is_stitched_back():
    # a long cell wraps onto a second │-line inside the SAME logical row (no ├──┼──┤
    # between them). Naively each physical line becomes its own row and the table
    # gains phantom rows with holes in them.
    src = """┌─────────────┬──────────────────┐
│ מודל small  │ שמע IDF נכון     │
│ במקום base  │ כש-base טעה      │
└─────────────┴──────────────────┘"""
    rows = rows_of(box_to_md(src))
    assert len(rows) == 1, f"wrapped lines became separate rows: {rows}"
    assert rows[0] == "| מודל small במקום base | שמע IDF נכון כש-base טעה |", \
        f"cell fragments not stitched: {rows[0]!r}"


def test_ragged_rows_are_padded_to_the_widest():
    # markdown requires every row to have the same cell count as the delimiter row;
    # a short row otherwise silently drops columns in the rendered <table>.
    src = """┌────┬────┬────┐
│ א  │ ב  │ ג  │
├────┼────┼────┤
│ ד  │
└────┴────┴────┘"""
    out = box_to_md(src)
    for line in rows_of(out):
        assert line.count("|") == 4, f"row has wrong cell count: {line!r}"


def test_pipe_inside_a_cell_is_escaped():
    # an unescaped | inside a cell splits it into two, shifting every later column
    src = """┌──────────┬──────────┐
│ a | b    │ ג        │
└──────────┴──────────┘"""
    out = box_to_md(src)
    assert r"a \| b" in out, "a literal | inside a cell must be escaped as \\|"


def test_single_column_box_is_left_alone():
    # one column is a frame/callout, not a table — converting it to markdown would
    # produce a degenerate one-column <table> instead of readable text
    src = """┌────────────────┐
│ הודעה חשובה    │
└────────────────┘"""
    out = box_to_md(src)
    assert "| --- |" not in out, "a single-column box must not become a table"


# --- what it must NOT touch --------------------------------------------------

def test_plain_markdown_table_is_untouched():
    # THE regression that matters most: the whole feature is gated on a real
    # U+2500-block character precisely so ordinary markdown tables pass through.
    # Note the |:---:| alignment row — re-parsing would treat it as a content row.
    src = "| רגיל | markdown |\n|:---:|---:|\n| a | b |"
    assert box_to_md(src) == src, "an ordinary markdown table was rewritten"


def test_box_table_inside_a_code_fence_is_untouched():
    # inside ``` the box art IS the content — converting it destroys the example
    src = "before\n```\n┌──┬──┐\n│ a │ b │\n└──┴──┘\n```\nafter"
    assert box_to_md(src) == src, "content inside a ``` fence was rewritten"


def test_prose_containing_a_box_character_is_untouched():
    # a sentence that merely mentions ─ must not be fenced or mangled mid-paragraph
    src = "התו ─ הוא קו אופקי, ואפשר לצייר איתו טבלאות."
    assert box_to_md(src) == src, "prose mentioning a box character was mangled"


def test_extract_py_tool_details_line_is_untouched():
    # extract.py emits tool output as ONE line of raw HTML (newlines are &#10;).
    # If that line happens to contain box characters and boxToMd rewrites it, the
    # <details> element is corrupted and the turn's DOM breaks.
    src = ('<h3 data-role="assistant">🤖 Claude</h3>\n\nטקסט\n\n'
           '<details class="tool"><summary>Bash</summary>'
           '<pre>┌──┬──┐&#10;│a │b │&#10;└──┴──┘</pre></details>\n\nסוף')
    assert box_to_md(src) == src, "extract.py's raw-HTML tool block was rewritten"


def test_text_with_no_box_characters_is_returned_verbatim():
    # the fast path — must be a true identity, not a reformat
    src = "# כותרת\n\nסתם טקסט עם **הדגשה** ורשימה:\n- א\n- ב\n"
    assert box_to_md(src) == src, "the no-box-characters fast path altered the text"


# --- box-drawn but not a table ----------------------------------------------

def test_tree_output_is_fenced_not_converted():
    # `├── src/` is box-drawn but is not a table. It can't become <table>, and left
    # as a paragraph RTL would scramble the indentation — so it gets fenced, where
    # `pre { direction: ltr }` preserves the alignment.
    src = "├── src/\n│   └── a.js"
    out = box_to_md(src)
    assert "```" in out, "box-drawn tree output was not fenced"
    assert "├── src/" in out, "tree content was altered"
    assert "| --- |" not in out, "tree output was wrongly turned into a table"


def test_lone_box_drawn_line_is_not_fenced():
    # fencing needs 2+ consecutive box-led lines; a single one is more likely prose
    src = "├── רק שורה אחת"
    assert box_to_md(src) == src, "a single box-led line should not be fenced"


if __name__ == "__main__":
    sys.exit(run.main([sys.argv[0], os.path.basename(__file__)[5:-3]]))
