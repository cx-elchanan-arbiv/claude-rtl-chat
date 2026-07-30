#!/usr/bin/env python3
"""Regression tests for sweep_uploads() — the uploads retention sweep.

WHY THIS EXISTS (2026-07-30): nothing ever deleted uploads, so every screenshot ever
pasted was still on disk (187MB). sweep_uploads() prunes them on startup.

This is the only code in the project that DELETES USER FILES, and it runs unattended
every time launchd restarts the server. The tests below are the guard rails:

  - it must only touch files matching the 8-hex prefix serve.py itself assigns.
    Anything a human put in uploads/ by hand — the דוח-כדאיות reports live there —
    must survive forever, no matter how old.
  - it must respect the retention window in both directions. A bug that flips the
    comparison deletes everything on the next restart.
  - UPLOAD_RETENTION_DAYS = 0 must disable it completely (the documented escape hatch).
  - it must never recurse into or delete directories.

Run:  python3 tests/test_uploads_sweep.py     (or: python3 tests/run.py)
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve  # noqa: E402

DAY = 86400


class sandbox:
    """Point serve.UPLOADS at a throwaway dir and set the retention window."""

    def __init__(self, retention_days):
        self.retention = retention_days

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="rtlchat-uploads-")
        self._saved = (serve.UPLOADS, serve.UPLOAD_RETENTION_DAYS)
        serve.UPLOADS = self.dir
        serve.UPLOAD_RETENTION_DAYS = self.retention
        return self

    def __exit__(self, *exc):
        serve.UPLOADS, serve.UPLOAD_RETENTION_DAYS = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def add(self, name, age_days=0):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write("x")
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
        return path

    def names(self):
        return sorted(os.listdir(self.dir))


def test_deletes_uploads_older_than_the_window():
    with sandbox(90) as s:
        s.add("1d7f8089_image.png", age_days=120)
        serve.sweep_uploads()
        assert s.names() == [], f"old upload survived the sweep: {s.names()}"


def test_keeps_uploads_inside_the_window():
    # off-by-one / flipped-comparison guard: a file one day short of the window stays
    with sandbox(90) as s:
        s.add("1d7f8089_image.png", age_days=89)
        serve.sweep_uploads()
        assert s.names() == ["1d7f8089_image.png"], \
            f"an upload inside the retention window was deleted: {s.names()}"


def test_never_touches_hand_placed_files():
    # THE important one. Files without the 8-hex prefix serve.py assigns were put
    # there by a human — the דוח-כדאיות reports are real examples. They must survive
    # regardless of age.
    with sandbox(30) as s:
        s.add("דוח-כדאיות-v4.html", age_days=400)
        s.add("notes.md", age_days=999)
        s.add("README", age_days=999)
        s.add("1d7f8089_image.png", age_days=400)     # ours — should go
        serve.sweep_uploads()
        assert s.names() == ["README", "notes.md", "דוח-כדאיות-v4.html"], \
            f"the sweep deleted a hand-placed file: {s.names()}"


def test_prefix_match_is_anchored_and_exact():
    # a name that merely CONTAINS 8 hex chars, or has a near-miss prefix, is not ours
    with sandbox(30) as s:
        s.add("photo-1d7f8089_x.png", age_days=400)   # hex not at the start
        s.add("1d7f808_image.png", age_days=400)      # 7 chars, too short
        s.add("1d7f8089zimage.png", age_days=400)     # no underscore separator
        s.add("1D7F8089_image.png", age_days=400)     # uppercase — serve.py emits lower
        serve.sweep_uploads()
        assert len(s.names()) == 4, \
            f"a near-miss filename was wrongly treated as ours: {s.names()}"


def test_retention_zero_disables_the_sweep_entirely():
    # the documented escape hatch — 0 must mean "never delete", not "delete everything"
    with sandbox(0) as s:
        s.add("1d7f8089_image.png", age_days=9999)
        serve.sweep_uploads()
        assert s.names() == ["1d7f8089_image.png"], \
            "UPLOAD_RETENTION_DAYS = 0 must disable the sweep"


def test_directories_are_never_removed():
    with sandbox(30) as s:
        d = os.path.join(s.dir, "1d7f8089_folder")
        os.mkdir(d)
        os.utime(d, (time.time() - 400 * DAY,) * 2)
        serve.sweep_uploads()
        assert os.path.isdir(d), "the sweep removed a directory"


def test_sweep_survives_a_missing_file_mid_run():
    # a live upload can be renamed or removed between listdir() and remove(); the
    # sweep must skip it rather than crash and abort the server's startup
    with sandbox(30) as s:
        s.add("1d7f8089_image.png", age_days=400)
        real_remove = os.remove
        calls = {"n": 0}

        def flaky_remove(path):
            calls["n"] += 1
            raise FileNotFoundError(path)

        os.remove = flaky_remove
        try:
            serve.sweep_uploads()      # must not raise
        finally:
            os.remove = real_remove
        assert calls["n"] == 1, "the sweep did not attempt the delete at all"


def test_empty_uploads_dir_is_a_no_op():
    with sandbox(30) as s:
        serve.sweep_uploads()
        assert s.names() == []


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import run
    sys.exit(run.main([sys.argv[0], os.path.basename(__file__)[5:-3]]))
