#!/usr/bin/env python3
"""Guards for the render pad's reading layout — the "I can't see the table" fix.

THE BUG (2026-08-03): a wide table pasted into the pad was unreadable. Four separate
causes, each of which can be silently reintroduced by a one-line CSS edit:

  1. #padView was capped at `max-width: 980px`, so "🔎 הסתר מקור" freed the space and
     then refused to use it — the result stayed 980px wide no matter the window.
  2. `#padView code { display: inline-block }` made every `route.ts:1084` an
     unbreakable box. A cell's `overflow-wrap: anywhere` cannot break INTO an
     inline-block, so one long token pushed the whole table past the pane.
  3. Nothing but #padView itself could scroll, so dragging a wide table sideways
     dragged the headings and prose along with it.
  4. The source pane was a fixed 32% with no way to shrink it, and both placeholder
     texts named the wrong side ("הדבק טקסט משמאל" — the source box is on the RIGHT;
     in an RTL flex row the first child lands right).

Most of this file is therefore static assertions about index.html rather than
behaviour: the failure mode here is a well-meaning edit, and a static guard is the
only thing that catches "someone put max-width back".

The numeric logic (zoom clamping, splitter clamping) and padWrapWide() are pure and
run for real under node, via jsbridge.

NOT covered here (needs a real browser — check by hand if you touch it): the pointer
drag on #padSplit, whether `position: sticky` actually pins the first column, and
whether Cmd +/- is swallowed before Chrome's own zoom sees it.

Run:  python3 tests/test_pad_view.py     (or: python3 tests/run.py)
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jsbridge  # noqa: E402
import run  # noqa: E402

INDEX = jsbridge.INDEX


# --------------------------------------------------------------------------
# a small CSS reader, so these tests survive reformatting of index.html
# --------------------------------------------------------------------------
def _index():
    with open(INDEX, encoding="utf-8") as fh:
        return fh.read()


def _style():
    blocks = re.findall(r"<style>(.*?)</style>", _index(), re.S)
    assert len(blocks) == 1, f"expected exactly one <style> block, found {len(blocks)}"
    return re.sub(r"/\*.*?\*/", "", blocks[0], flags=re.S)   # comments hold braces too


def _norm(s):
    return " ".join(s.split())


def _rules():
    """[(selector, body)] for every top-level rule. @-rules are skipped whole."""
    css, out, i, n = _style(), [], 0, 0
    n = len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            break
        sel, depth, k = css[i:j].strip(), 0, j
        while k < n:
            if css[k] == "{":
                depth += 1
            elif css[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if not sel.startswith("@"):
            out.append((_norm(sel), css[j + 1:k]))
        i = k + 1
    return out


def _props(body):
    out = {}
    for part in body.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[_norm(k)] = _norm(v)
    return out


def decls(selector):
    """Declarations of the first rule whose selector LIST contains `selector`."""
    want = _norm(selector)
    for sel, body in _rules():
        if want in [_norm(p) for p in sel.split(",")]:
            return _props(body)
    raise AssertionError(
        f"no CSS rule for {selector!r} in index.html — renamed or deleted? "
        f"If the rule moved on purpose, update this test; if it vanished, the "
        f"behaviour it carried probably vanished with it."
    )


def rule_containing(needle):
    """(selector, props) of the first rule whose body mentions `needle`."""
    for sel, body in _rules():
        if needle in body:
            return sel, _props(body)
    raise AssertionError(f"no CSS rule in index.html mentions {needle!r}")


# --------------------------------------------------------------------------
# 1. the result gets the width
# --------------------------------------------------------------------------
def test_padview_is_not_width_capped():
    # the original bug: hiding the source freed the space and #padView ignored it
    assert "max-width" not in decls("#padView"), (
        "#padView must not carry its own max-width — that is what made "
        "'הסתר מקור' useless. Cap the prose elements instead (--pad-measure)."
    )


def test_prose_keeps_a_measure_and_tables_do_not():
    sel, props = rule_containing("var(--pad-measure)")
    assert props.get("margin-inline") == "auto", \
        f"the measure rule must centre what it caps, got {props!r}"
    for wide in ("table", "pre"):
        assert not re.search(r"\b%s\b" % wide, sel), (
            f"{wide} is inside the prose-measure selector ({sel!r}) — that re-caps "
            f"exactly the elements this feature exists to let out to full width"
        )
    assert "--pad-measure" in decls("#pad"), "--pad-measure is not defined on #pad"


def test_the_measure_rule_outranks_the_heading_rules():
    # #padView h1 sets `margin: .8em 0 .35em`, which includes the inline axis. At
    # equal specificity the LAST rule wins, so a measure rule with a single id is
    # one reordering away from silently losing margin-inline on every heading.
    sel, _ = rule_containing("var(--pad-measure)")
    assert sel.count("#") >= 2, (
        f"the prose-measure selector {sel!r} has fewer than two ids, so it only wins "
        f"by source order against `#padView h1 {{ margin: ... }}`. Keep it at "
        f"`#pad #padView > :is(...)` or the headings stop centring when rules move."
    )


# --------------------------------------------------------------------------
# 2. inline code must be breakable
# --------------------------------------------------------------------------
def test_inline_code_can_break_across_lines():
    for sel in ("#padView code", "#content code"):
        props = decls(sel)
        assert props.get("display") != "inline-block", (
            f"{sel} is inline-block again — a table cell's overflow-wrap cannot break "
            f"into an inline-block, so one long `path/file.ts:12` token drags the whole "
            f"table past the pane. Use `display: inline`."
        )
        assert props.get("unicode-bidi") == "isolate", (
            f"{sel} needs unicode-bidi: isolate — it is what keeps an LTR code run from "
            f"reordering its RTL neighbours once display is no longer inline-block."
        )


# --------------------------------------------------------------------------
# 3. wide blocks scroll by themselves
# --------------------------------------------------------------------------
def test_the_document_itself_never_scrolls_sideways():
    assert decls("#padView").get("overflow-x") == "hidden", (
        "#padView must not scroll horizontally — if it does, dragging a wide table "
        "sideways drags every heading and paragraph with it. .xscroll owns that axis."
    )


def test_xscroll_is_the_horizontal_scroller():
    props = decls("#padView .xscroll")
    assert props.get("overflow-x") == "auto", ".xscroll must scroll horizontally"
    assert props.get("max-width") == "100%", \
        ".xscroll without max-width grows to its content and overflows #padView instead"


def test_xscroll_has_a_visible_scrollbar():
    # macOS hides overlay scrollbars until you scroll, so the bar IS the only hint
    # that there is more table off to the side.
    assert decls("#padView .xscroll::-webkit-scrollbar").get("height"), \
        "the webkit scrollbar needs an explicit height or Chrome keeps it as an overlay"
    # …and Chrome IGNORES every ::-webkit-scrollbar rule on an element that also sets
    # the standard `scrollbar-width`. Adding it back silently restores the invisible
    # overlay bar and the styling above becomes dead code. (Measured: with it set,
    # offsetHeight - clientHeight is 0; without it, 10px.)
    assert "scrollbar-width" not in decls("#padView .xscroll"), (
        "scrollbar-width on .xscroll makes Chrome drop the ::-webkit-scrollbar styling "
        "and go back to an overlay bar that is invisible until you already scrolled"
    )


def test_pre_does_not_scroll_inside_its_own_wrapper():
    # two nested auto-scrollers = the inner one wins and .xscroll never moves, which
    # also loses the LTR start-edge fix that .xscroll.xltr exists for
    assert "overflow-x" not in decls("#padView pre"), \
        "#padView pre must leave the horizontal scrolling to its .xscroll wrapper"
    assert decls("#padView .xscroll.xltr").get("direction") == "ltr", \
        "a code block's scroller must be LTR or it opens on the END of every line"


def test_the_sticky_first_column_is_opaque():
    props = decls("#padView .xscroll > table td:first-child")
    assert props.get("position") == "sticky", \
        "the label column must stay put while the table scrolls sideways"
    assert props.get("inset-inline-start") == "0", \
        "in RTL the scroller starts at the right edge — pin with inset-inline-start"
    assert props.get("background-color"), (
        "a sticky cell with no background lets the scrolling cells show straight "
        "through it"
    )


# --------------------------------------------------------------------------
# 4. controls, wiring and the side the panes are actually on
# --------------------------------------------------------------------------
def test_the_zoom_multiplier_reaches_padview():
    fs = decls("#padView").get("font-size", "")
    assert "var(--pad-zoom" in fs, (
        f"#padView font-size is {fs!r} — without --pad-zoom in it the A+/A− buttons "
        f"and Cmd +/- change a variable nothing reads."
    )


def test_split_min_matches_the_css_min_width():
    # a drag can report 120px, JS clamps to SPLIT_MIN, CSS refuses anything under its
    # own min-width — the pane then sticks and the saved value never matches reality
    js = re.search(r"const SPLIT_MIN\s*=\s*(\d+)", _index())
    assert js, "const SPLIT_MIN vanished from the pad script"
    css = decls("#padSrc").get("min-width", "")
    assert css == js.group(1) + "px", (
        f"SPLIT_MIN is {js.group(1)}px but #padSrc min-width is {css!r} — the splitter "
        f"and the layout disagree about how narrow the source pane may get"
    )


def test_the_source_pane_width_is_a_variable():
    assert "var(--pad-src" in decls("#padSrc").get("flex", ""), \
        "#padSrc must take its width from --pad-src, or the splitter has nothing to set"


def test_the_pad_controls_exist():
    html = _index()
    for el_id in ("padSplit", "padZoomIn", "padZoomOut", "padZoomVal",
                  "padCompact", "padWrapPre", "padToggleSrc"):
        assert 'id="%s"' % el_id in html, f"#{el_id} is gone from the pad toolbar"


def test_the_pad_remembers_its_settings():
    html = _index()
    for key in ("rtl-pad-zoom", "rtl-pad-split", "rtl-pad-hidesrc",
                "rtl-pad-compact", "rtl-pad-wrappre"):
        assert key in html, f"{key} is no longer persisted — the pad resets every open"


def test_the_placeholders_name_the_right_side():
    # #padSrc is the FIRST child of an RTL flex row, so it renders on the RIGHT and
    # the result on the LEFT. Both strings used to say the opposite.
    html = _index()
    src_ph = re.search(r'id="padSrc"[^>]*placeholder="([^"]*)"', html)
    assert src_ph, "the source textarea lost its placeholder"
    assert "משמאל" in src_ph.group(1), (
        f"the source box sits on the right, so its placeholder must point the reader "
        f"LEFT to the result; got {src_ph.group(1)!r}"
    )
    for ph in re.findall(r'<p class="ph">([^<]*)</p>', html):
        if "הדבק" in ph:
            assert "מימין" in ph, (
                f"the result pane sits on the left, so its placeholder must point RIGHT "
                f"to the source box; got {ph!r}"
            )


# --------------------------------------------------------------------------
# the pure JS, run for real
# --------------------------------------------------------------------------
SYMBOLS = ["ZOOM_MIN", "SPLIT_MIN", "padClampZoom", "padZoomStep", "padClampSplit",
           "padWrapWide"]

# the handful of DOM methods padWrapWide() touches, and nothing else
FAKE_DOM = """
const DOC = { createElement: t => mk(t) };
function mk(tag, kids) {
  const n = {
    tagName: tag.toUpperCase(), className: '', parentNode: null,
    children: kids || [], ownerDocument: DOC,
    insertBefore(node, ref) {
      const i = this.children.indexOf(ref);
      this.children.splice(i < 0 ? this.children.length : i, 0, node);
      node.parentNode = this;
    },
    appendChild(node) {
      if (node.parentNode) {
        const c = node.parentNode.children, i = c.indexOf(node);
        if (i >= 0) c.splice(i, 1);
      }
      this.children.push(node);
      node.parentNode = this;
    },
    querySelectorAll(sel) {
      const want = sel.split(',').map(s => s.trim().toUpperCase()), out = [];
      const walk = x => { for (const k of x.children) { if (want.includes(k.tagName)) out.push(k); walk(k); } };
      walk(this);
      return out;                      // a static list, like the real one
    },
  };
  for (const k of n.children) k.parentNode = n;
  return n;
}
function shape(n) { return { tag: n.tagName, cls: n.className, kids: n.children.map(shape) }; }
"""


def js(script, fake_dom=False):
    if not jsbridge.node_available():
        raise run.SkipTest("node is not installed")
    return jsbridge.run(SYMBOLS, (FAKE_DOM if fake_dom else "") + script)


def test_zoom_is_clamped_to_a_sane_range():
    out = js("console.log(JSON.stringify(["
             "padClampZoom(0.01), padClampZoom(99), padClampZoom(1.3),"
             "padClampZoom('nonsense'), padClampZoom(null), padClampZoom(-2)]));")
    lo, hi, ok, junk, empty, neg = out
    assert lo == 0.6, f"a tiny zoom must clamp to the floor, got {lo}"
    assert hi == 2, f"a huge zoom must clamp to the ceiling, got {hi}"
    assert ok == 1.3, f"a legal zoom must pass through untouched, got {ok}"
    # localStorage is a string store and hand-editable; garbage must not blank the pad
    assert junk == 1 and empty == 1 and neg == 1, \
        f"unreadable stored zoom must fall back to 100%, got {out!r}"


def test_zoom_steps_stay_on_clean_values():
    out = js("let z = 1; const seen = [];"
             "for (let i = 0; i < 3; i++) { z = padZoomStep(z, -1); seen.push(z); }"
             "console.log(JSON.stringify(seen));")
    assert out == [0.9, 0.8, 0.7], (
        f"stepping must land on clean 2-decimal values — got {out!r}. Float drift here "
        f"ends up in the DOM and in localStorage as 0.7000000000000001."
    )


def test_zoom_cannot_step_past_its_bounds_or_lose_reset():
    out = js("let lo = 1, hi = 1;"
             "for (let i = 0; i < 20; i++) { lo = padZoomStep(lo, -1); hi = padZoomStep(hi, +1); }"
             "console.log(JSON.stringify([lo, hi, padZoomStep(1.7, 0), padZoomStep(0.6, 0)]));")
    lo, hi, reset_hi, reset_lo = out
    assert lo == 0.6 and hi == 2, f"holding A−/A+ must stop at the bounds, got {lo}/{hi}"
    assert reset_hi == 1 and reset_lo == 1, "dir 0 must reset to 100% from either side"


def test_splitter_respects_its_floor_and_ceiling():
    out = js("console.log(JSON.stringify(["
             "padClampSplit(50, 1000), padClampSplit(900, 1000), padClampSplit(300, 1000)]));")
    floor, ceiling, mid = out
    assert floor == 180, f"the source pane must not drag below SPLIT_MIN, got {floor}"
    assert ceiling == 600, f"the source pane must not eat the result, got {ceiling}"
    assert mid == 300, f"a width between the bounds passes through, got {mid}"


def test_splitter_returns_null_before_layout():
    # openPad() restores the saved width immediately; if #padMain has not been laid
    # out yet its width is 0 and any clamp against it would write a bogus value
    out = js("console.log(JSON.stringify(["
             "padClampSplit(300, 0), padClampSplit(300, undefined), padClampSplit('x', 900)]));")
    assert out == [None, None, None], \
        f"an unmeasurable pad must yield null so the caller skips the write, got {out!r}"


def test_nothing_saved_leaves_the_default_width_alone():
    # openPad() restores with Number(localStorage.getItem(...)), and Number(null) is 0
    # — NOT NaN. Treating that 0 as a drag clamps it up to SPLIT_MIN, so a pad that had
    # never been dragged opened with its source pane jammed to the minimum.
    out = js("console.log(JSON.stringify([padClampSplit(0, 2000), padClampSplit(Number(null), 2000)]));")
    assert out == [None, None], (
        f"an empty saved width must yield null so the CSS default (26%) stands, got "
        f"{out!r} — clamping it to the floor pins the source pane at its minimum on "
        f"the first ever open"
    )


def test_wide_blocks_each_get_their_own_scroller():
    out = js("const root = mk('div', [mk('p'), mk('table'), mk('pre')]);"
             "const n = padWrapWide(root);"
             "console.log(JSON.stringify({ n, tree: shape(root) }));", fake_dom=True)
    assert out["n"] == 2, f"a table and a pre are two wide blocks, wrapped {out['n']}"
    kids = out["tree"]["kids"]
    assert [k["tag"] for k in kids] == ["P", "DIV", "DIV"], \
        f"the paragraph must be left alone and each wide block wrapped: {kids!r}"
    assert kids[1]["cls"] == "xscroll" and kids[1]["kids"][0]["tag"] == "TABLE"
    assert kids[2]["cls"] == "xscroll xltr", (
        f"a code block's scroller needs the xltr class or it opens scrolled to the end "
        f"of every line; got {kids[2]['cls']!r}"
    )
    assert kids[2]["kids"][0]["tag"] == "PRE", "the pre was not moved inside its wrapper"


def test_wrapping_twice_changes_nothing():
    # renderPad() re-renders on every keystroke; a wrapper nested in a wrapper would
    # accumulate a scroller per character typed
    out = js("const root = mk('div', [mk('table')]);"
             "padWrapWide(root);"
             "const again = padWrapWide(root);"
             "console.log(JSON.stringify({ again, tree: shape(root) }));", fake_dom=True)
    assert out["again"] == 0, f"a second pass must wrap nothing, wrapped {out['again']}"
    assert out["tree"]["kids"][0]["kids"][0]["tag"] == "TABLE", \
        f"the table gained a second wrapper: {out['tree']!r}"


def test_a_document_with_nothing_wide_is_untouched():
    out = js("const root = mk('div', [mk('p'), mk('h2'), mk('ul', [mk('li')])]);"
             "const n = padWrapWide(root);"
             "console.log(JSON.stringify({ n, tree: shape(root) }));", fake_dom=True)
    assert out["n"] == 0, "prose must not be wrapped in a scroller"
    assert [k["tag"] for k in out["tree"]["kids"]] == ["P", "H2", "UL"], \
        f"the prose tree was rearranged: {out['tree']!r}"


if __name__ == "__main__":
    sys.exit(run.main([sys.argv[0], os.path.basename(__file__)[5:-3]]))
