# Tests

```sh
python3 tests/run.py            # everything
python3 tests/run.py box        # only files matching "box"
python3 tests/test_box_tables.py
```

No dependencies. No `pip install`, no `npm install`, no virtualenv — the same
constraint the rest of the project holds to. Node is needed for the browser-side
tests; without it they report as skipped rather than failing.

`pytest tests/` also works if you happen to have pytest — the test functions follow
its naming convention. `tests/run.py` is the fallback that always works.

## What's covered

| File | Guards |
| --- | --- |
| `test_box_tables.py` | `boxToMd()` — box-drawing tables → real markdown tables, and the much longer list of things it must **not** rewrite |
| `test_rich_copy.py` | `tableToMd()` — copying a rendered table without losing the table |
| `test_uploads_sweep.py` | `sweep_uploads()` — the only code here that deletes user files |
| `test_html_injection.py` | The "one literal `<tag>` swallows the page" bug, turn-boundary detection, and the atomic-write race |
| `test_extract_index.py` | The session index: real-activity time vs. the file's mtime, `origin`, the `bg` flag, and liveness that ignores the daemon's plumbing |
| `test_view_filter.py` | `actAt()` / `isWeb()` / `inView()` / `originNote()` — the tab-strip view filter's whole rule set |

Each test file opens with the bug it exists to prevent. Read that header before
changing the code it covers — most of these guard against a specific failure that
already happened once in production.

## Testing the browser code from Python

`index.html` is one self-contained file with no build step, so its JS can't be
imported. `jsbridge.py` pulls the functions out of `index.html` **by symbol name**
and runs them under `node`. Reformatting `index.html` is safe; renaming or deleting
a covered symbol fails loudly, which is deliberate — those helpers are load-bearing
for RTL rendering and a rename should force you to look at the test.

`jsbridge` only reaches pure functions. Anything that needs a real DOM or clipboard
— `copyRich()`'s off-screen clone, the `ClipboardItem` write — is called out in the
relevant test file's header as needing a manual check.

## Adding a test

Drop a `test_*.py` in this directory with `test_*` functions and plain `assert`s.
`run.py` finds it automatically. Two conventions worth keeping:

- **Write the failure message, not just the assertion.** `assert x == y, "why this
  matters"` — the message is what someone reads at 2am when it breaks.
- **State the bug in the module docstring.** A test whose reason for existing is
  obvious from its name doesn't need it; one guarding a subtle production failure
  does, or it gets deleted by whoever finds it inconvenient later.

## Verifying the suite still bites

A test that can't fail is worse than no test. To check a guard is real, break the
code it covers on purpose and confirm the right test goes red:

```sh
# e.g. make boxToMd ignore ``` fences, then:
python3 tests/run.py box        # expect test_box_table_inside_a_code_fence... to fail
git checkout index.html
```
