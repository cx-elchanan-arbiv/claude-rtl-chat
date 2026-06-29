#!/usr/bin/env python3
"""Verify the precise live-terminal signal works on this machine.
Run:  python3 _diag.py
If it prints session ids under 'live_session_ids', the fix is fully active.
If it prints 'None (fell back to count heuristic)', claude does NOT hold the
transcript fd open here and we keep the old behavior."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract

pids = extract._claude_pids()
print("claude pids:", pids)
ids = extract.live_session_ids()
if ids is None:
    print("live_session_ids -> None (fell back to count heuristic)")
    print("live_counts ->", extract.live_counts())
else:
    print("live_session_ids ->", sorted(ids) or "(empty: no live terminals)")
