#!/usr/bin/env python3
"""Zero-dependency test runner for claude-rtl-chat.

This project deliberately has no build step, no npm and no virtualenv — it is a
single serve.py plus a single index.html you can drop on any Mac. The test suite
holds the same line: `python3 tests/run.py` and nothing to install.

The test functions are named `test_*` so `pytest tests/` also works if you happen
to have it; this runner is the fallback that always works.

Usage:
    python3 tests/run.py                  # everything
    python3 tests/run.py box              # only files whose name matches "box"
    python3 tests/test_box_tables.py      # a single file directly
"""
import os
import sys
import traceback

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def run_module(mod):
    """Run every `test_*` callable in `mod`. Returns the number of failures."""
    names = sorted(n for n in dir(mod) if n.startswith("test_") and callable(getattr(mod, n)))
    failures = 0
    skipped = 0
    for name in names:
        try:
            getattr(mod, name)()
        except SkipTest as e:
            print(f"  {YELLOW}~{RESET} {name} {DIM}(skipped: {e}){RESET}")
            skipped += 1
        except Exception:
            print(f"  {RED}✗{RESET} {name}")
            print(DIM + traceback.format_exc().rstrip() + RESET)
            failures += 1
        else:
            print(f"  {GREEN}✓{RESET} {name}")
    return failures, len(names) - failures - skipped, skipped


class SkipTest(Exception):
    """Raise from a test to skip it (missing optional dependency, etc.)."""


def main(argv):
    # repo root on the path so tests can `import serve` / `import extract`
    sys.path.insert(0, REPO)
    sys.path.insert(0, TESTS)

    pattern = argv[1] if len(argv) > 1 else ""
    files = sorted(f for f in os.listdir(TESTS)
                   if f.startswith("test_") and f.endswith(".py") and pattern in f)
    if not files:
        print(f"no test files match {pattern!r}")
        return 1

    total_fail = total_pass = total_skip = 0
    for fname in files:
        print(f"\n{fname}")
        mod_name = fname[:-3]
        try:
            mod = __import__(mod_name)
        except Exception:
            print(f"  {RED}✗{RESET} could not import {mod_name}")
            print(DIM + traceback.format_exc().rstrip() + RESET)
            total_fail += 1
            continue
        f, p, s = run_module(mod)
        total_fail, total_pass, total_skip = total_fail + f, total_pass + p, total_skip + s

    skip_note = f", {total_skip} skipped" if total_skip else ""
    if total_fail:
        print(f"\n{RED}✗ {total_fail} failed{RESET}, {total_pass} passed{skip_note}")
        return 1
    print(f"\n{GREEN}✓ {total_pass} passed{RESET}{skip_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
