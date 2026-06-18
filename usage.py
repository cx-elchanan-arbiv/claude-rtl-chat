#!/usr/bin/env python3
"""Best-effort fetch of the real Claude Max plan usage from claude.ai.

Decrypts the Chrome `sessionKey` cookie, calls
  GET https://claude.ai/api/organizations/{uuid}/usage
(discovered endpoint) and writes usage.json for the RTL mirror footer.

Everything is wrapped so failure is silent — the page soft-falls-back to the
local token counter when usage.json is missing or stale (>10 min old)."""
import hashlib
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import time
import urllib.request

HOME = os.path.expanduser("~")
COOKIES = f"{HOME}/Library/Application Support/Google/Chrome/Default/Cookies"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.json")
# Must look like a real Chrome — Cloudflare binds the cf_clearance cookie to the UA.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def chrome_key():
    pw = subprocess.run(["security", "find-generic-password", "-s",
                         "Chrome Safe Storage", "-w"],
                        capture_output=True, text=True).stdout.strip()
    return hashlib.pbkdf2_hmac("sha1", pw.encode(), b"saltysalt", 1003, 16)


def _decrypt(val, aes_key):
    from Crypto.Cipher import AES
    if not val:
        return ""
    if val[:3] == b"v10":
        dec = AES.new(aes_key, AES.MODE_CBC, b" " * 16).decrypt(val[3:])
        dec = dec[:-dec[-1]]
        try:
            return dec.decode("utf-8")
        except UnicodeDecodeError:
            return dec[32:].decode("utf-8", "replace")  # newer Chrome 32-byte prefix
    return val.decode("utf-8", "replace")


def cookie_header():
    """Full claude.ai cookie jar — Cloudflare needs cf_clearance/__cf_bm, not just
    sessionKey, or it returns 403."""
    tmp = "/tmp/_rtl_ck.sqlite"
    shutil.copy(COOKIES, tmp)
    con = sqlite3.connect(tmp)
    rows = con.execute("select name, encrypted_value from cookies "
                       "where host_key like '%claude.ai%'").fetchall()
    con.close()
    os.remove(tmp)
    if not rows:
        return None
    aes_key = chrome_key()
    jar = {n: _decrypt(v, aes_key) for n, v in rows}
    if not jar.get("sessionKey"):
        return None
    return "; ".join(f"{n}={v}" for n, v in jar.items() if v)


def get(url, cookie):
    req = urllib.request.Request(url, headers={
        "Cookie": cookie, "User-Agent": UA, "Accept": "*/*",
        "Referer": "https://claude.ai/", "Accept-Language": "en-US,en;q=0.9",
        "anthropic-client-platform": "web_claude_ai"})
    with urllib.request.urlopen(req, context=ssl.create_default_context(),
                                timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    cookie = cookie_header()
    if not cookie:
        return
    orgs = get("https://claude.ai/api/organizations", cookie)
    uuid = orgs[0]["uuid"]
    u = get(f"https://claude.ai/api/organizations/{uuid}/usage", cookie)
    fh = u.get("five_hour") or {}
    wk = u.get("seven_day") or {}
    payload = {
        "percent_session": round(fh.get("utilization", 0)),
        "reset_session": fh.get("resets_at"),
        "percent_week": round(wk.get("utilization", 0)),
        "reset_week": wk.get("resets_at"),
        "fetched_at": int(time.time()),
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
