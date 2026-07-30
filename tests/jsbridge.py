#!/usr/bin/env python3
"""Run the browser-side JS from index.html under node, so it can be unit-tested.

index.html is a single self-contained file with no build step and no npm — the JS
lives in inline <script> tags. Rather than duplicate that logic into a test fixture
(which would drift the moment someone edits the real thing), we pull the actual
symbols out of index.html by name and execute them in node.

Extraction is by SYMBOL NAME, not line number, so reformatting index.html is safe.
Renaming or deleting a symbol fails loudly here — which is the point: these helpers
are load-bearing for RTL rendering, and a rename should force you to look at the test.
"""
import json
import os
import shutil
import subprocess
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO, "index.html")

_OPEN = "([{"
_CLOSE = ")]}"


def node_available():
    """These tests need node. Absent (fresh machine / CI without it) → skip, not fail."""
    return shutil.which("node") is not None


def _read_index():
    with open(INDEX, encoding="utf-8") as fh:
        return fh.read()


def _extract_function(src, name):
    """Pull `function name(...) { ... }` out by brace-matching its body."""
    marker = "function " + name + "("
    i = src.find(marker)
    if i < 0:
        raise LookupError(
            f"function {name}() not found in index.html — was it renamed or removed? "
            f"If it moved, update the symbol name in tests/; if it was deleted on "
            f"purpose, delete the test that covers it."
        )
    body = src.index("{", i)
    depth = 0
    for j in range(body, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise LookupError(f"unbalanced braces while extracting function {name}()")


def _extract_const(src, name):
    """Pull `const name = ...;` out, scanning to the `;` that ends the statement.

    Bracket-aware so a `;` nested inside (), [] or {} doesn't end it early.
    """
    marker = "const " + name
    i = src.find(marker)
    if i < 0:
        raise LookupError(
            f"const {name} not found in index.html — was it renamed or removed? "
            f"See the note in _extract_function()."
        )
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif c == ";" and depth == 0:
            return src[i:j + 1]
    raise LookupError(f"no statement terminator found while extracting const {name}")


def extract(symbols):
    """Return the source of the named symbols, in the order given.

    A name starting with an uppercase letter is treated as a `const`, otherwise a
    `function` — matching the convention index.html already uses (BOX_ANY vs boxToMd).
    Lowercase consts (isRule, isRow) are tried as functions first, then as consts.
    """
    src = _read_index()
    out = []
    for name in symbols:
        if name[0].isupper():
            out.append(_extract_const(src, name))
        else:
            try:
                out.append(_extract_function(src, name))
            except LookupError:
                out.append(_extract_const(src, name))
    return "\n".join(out)


def run(symbols, script):
    """Execute `script` in node with `symbols` (from index.html) in scope.

    The script should end by calling console.log(JSON.stringify(...)); whatever it
    prints on the LAST line is parsed as JSON and returned.
    """
    source = extract(symbols) + "\n" + script
    fd, path = tempfile.mkstemp(suffix=".mjs", prefix="rtlchat-test-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(source)
        proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    finally:
        os.remove(path)
    if proc.returncode != 0:
        raise AssertionError(
            f"node exited {proc.returncode}\n--- stderr ---\n{proc.stderr.strip()}"
        )
    last = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not last:
        raise AssertionError(f"script printed nothing\n--- stderr ---\n{proc.stderr.strip()}")
    return json.loads(last[-1])
