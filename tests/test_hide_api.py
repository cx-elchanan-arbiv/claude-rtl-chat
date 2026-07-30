#!/usr/bin/env python3
"""Regression tests for hiding a conversation from the history strip (serve.py side).

WHY THIS EXISTS (2026-07-30): the ✕ on a history tab had to answer one question honestly —
what does "delete from history" delete? The answer this code commits to:

  NOTHING on disk. The transcripts under ~/.claude/projects belong to Claude Code, not to
  us: that same terminal can still `--resume` those sessions. Hiding writes an id into a
  local list (hidden.json) that extract.py skips, and /unhide takes it straight back out.
  extract's side of that contract is covered in test_extract_index.py.

The rules worth guarding here:
  - a round-trip through the list survives, and a corrupt/absent file degrades to "nothing
    hidden" rather than throwing on every request
  - an OWNED chat cannot be hidden. It has its own ✕ (close → drops to history), and hiding
    one would make a chat we still hold — and can still send to — disappear from every strip
    while owned.json says it's alive

Run:  python3 tests/test_hide_api.py     (or: python3 tests/run.py)
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402
import serve  # noqa: E402

SID = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class Box:
    """serve's state files in a temp dir."""

    KEYS = ("BASE", "HIDDEN", "OWNED")

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rtl-hide-")
        self.saved = {k: getattr(serve, k) for k in self.KEYS}
        serve.BASE = self.dir
        serve.HIDDEN = os.path.join(self.dir, "hidden.json")
        serve.OWNED = os.path.join(self.dir, "owned.json")
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(serve, k, v)
        shutil.rmtree(self.dir, ignore_errors=True)

    def set_owned(self, d):
        with open(serve.OWNED, "w", encoding="utf-8") as fh:
            json.dump(d, fh)

    def write_raw_hidden(self, text):
        with open(serve.HIDDEN, "w", encoding="utf-8") as fh:
            fh.write(text)


def test_hidden_list_round_trips():
    with Box():
        assert serve.read_hidden() == [], "no file yet → nothing hidden"
        serve.write_hidden([SID, OTHER])
        assert serve.read_hidden() == [SID, OTHER]
        serve.write_hidden([OTHER])                      # what /unhide leaves behind
        assert serve.read_hidden() == [OTHER]
        serve.write_hidden([])                           # what /unhide-all leaves behind
        assert serve.read_hidden() == []


def test_a_corrupt_hidden_file_hides_nothing():
    """It's written on every ✕ while a 1s render loop reads it. A half-written or
    hand-edited file must not throw on every request — worst case, nothing is hidden."""
    with Box() as box:
        box.write_raw_hidden('["11111111-1111-1111-1111-1')      # truncated mid-write
        assert serve.read_hidden() == [], "a corrupt list must degrade to empty, not raise"
        box.write_raw_hidden("null")
        assert serve.read_hidden() == [], "null must behave like an empty list"


def test_an_owned_chat_cannot_be_hidden():
    """The one hard rule: hiding is for CLOSED conversations only."""
    with Box() as box:
        box.set_owned({SID: {"cwd": "/tmp", "perm": "read"}})
        assert serve.can_hide(SID) is False, \
            "an owned chat must be refused — it's writable, and its ✕ means 'close', not 'hide'"
        assert serve.can_hide(OTHER) is True, "a chat we don't own is fair game"


def test_a_chat_becomes_hideable_once_it_is_closed():
    """The intended flow: ✕ on an open chat closes it (owned.json entry goes) and only then
    may it be hidden from history."""
    with Box() as box:
        box.set_owned({SID: {"cwd": "/tmp", "perm": "read"}})
        assert serve.can_hide(SID) is False
        box.set_owned({})                                # what POST /close does
        assert serve.can_hide(SID) is True, \
            "after closing, hiding must become available (that's the documented way out)"


if __name__ == "__main__":
    sys.exit(run.main([sys.argv[0], os.path.basename(__file__)[5:-3]]))
