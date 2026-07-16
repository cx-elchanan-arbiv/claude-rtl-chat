#!/usr/bin/env python3
"""Regression test for the "one literal tag swallows the page" bug.

THE BUG (2026-07-16): the conversation text is rendered as Markdown by marked, which
passes raw inline HTML straight through to the DOM. When Claude or the user typed a
literal tag while *discussing* rendering — e.g. `<code dir="ltr">` in a sentence about
RTL — and it wasn't closed, marked turned it into a real element that swallowed every
turn after it (~337KB in the wild). Symptom: everything below becomes one monospace,
LTR, border-boxed block with no user/Claude separation. It hit THIS project hardest
because its conversations are literally about html/rtl/rendering.

THE FIX: extract.md_text() escapes `<` → `&lt;` in the message TEXT only (keeping all
Markdown intact and leaving extract's own structural html — headers, tool <details> —
live). The streaming/interrupted paths in index.html apply the same disarm client-side.

Run:  python3 test_html_injection.py       (exit 0 = pass)
"""
import json
import os
import tempfile

import extract


def _write_jsonl(events):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


def _assistant(text):
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": text}],
                        "usage": {"output_tokens": 1}}}


def _user(text):
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def test_unclosed_tag_is_neutralized():
    # the exact shape that broke: an assistant turn explaining RTL with a literal,
    # unclosed <code dir="ltr">, followed by MORE turns that used to get swallowed.
    poison = 'כל מזהה-קוד בתוך פרוזה עברית: <code dir="ltr"> עם unicode-bidi:isolate.'
    path = _write_jsonl([
        _user("שאלה ראשונה"),
        _assistant(poison),
        _user("שאלה שנייה — הטקסט הזה נבלע לפני התיקון"),
        _assistant("תשובה אחרונה — גם היא נבלעה"),
    ])
    try:
        md, turns, *_ = extract.render(path)
    finally:
        os.remove(path)

    # 1. no ACTIVE unbalanced <code> tag survives (would swallow the page)
    assert md.count("<code") == md.count("</code>"), \
        f"unbalanced <code>: {md.count('<code')} open vs {md.count('</code>')} close"
    # 2. the literal tag is present but DISARMED (visible as text, not a live element)
    assert "&lt;code" in md, "the literal <code should be escaped to &lt;code"
    # 3. all four turns still render (nothing got swallowed): 2 user + 2 assistant headers
    assert turns == 4, f"expected 4 turn headers, got {turns} (content was swallowed?)"
    # 4. the last turn's text survived
    assert "תשובה אחרונה" in md, "last turn missing — swallowed"


def test_normal_markdown_still_works():
    # the disarm must NOT break legit Markdown (bold, lists, inline `code`, headers)
    path = _write_jsonl([_assistant("**מודגש** ורשימה:\n- א\n- ב\nו-`inline` קוד.")])
    try:
        md, *_ = extract.render(path)
    finally:
        os.remove(path)
    assert "**מודגש**" in md and "`inline`" in md and "- א" in md, \
        "md_text must leave Markdown untouched — only `<` is escaped"


def test_content_heading_is_not_a_turn_boundary():
    # a `### heading` Claude writes INSIDE a reply must not be counted as a new turn
    # (it used to: the renderer sniffed every <h3>). Only extract's own role headers count.
    path = _write_jsonl([
        _user("שאלה"),
        _assistant("פתיח.\n\n### דעתי\nגוף.\n\n### מה שנראתה לי\nעוד גוף."),  # 2 content headings
    ])
    try:
        md, turns, *_ = extract.render(path)
    finally:
        os.remove(path)
    # exactly 2 real turns (1 user + 1 assistant) — the 2 content ### headings don't add turns
    assert turns == 2, f"content headings inflated the turn count: got {turns}, expected 2"
    assert md.count('<h3 data-role=') == 2, "should be exactly 2 role-tagged turn headers"
    # content headings stay as Markdown `###` (marked renders them to plain <h3> in the
    # browser; decorateTurns ignores them). The "נראתה" one (contains substring אתה) must
    # NOT have become a role header — the old text-sniff would have made it a user bubble.
    assert "### דעתי" in md and "### מה שנראתה לי" in md, \
        "content ### headings must remain Markdown, not be turned into role headers"


if __name__ == "__main__":
    test_unclosed_tag_is_neutralized()
    test_normal_markdown_still_works()
    test_content_heading_is_not_a_turn_boundary()
    print("✓ all passed — html-injection swallow bug + turn-boundary integrity hold")
