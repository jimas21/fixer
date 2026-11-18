"""Pair a phone with Fixer Desktop, list paired phones, or remove one.

    python pair.py "the owner's iPhone"            new phone: shows a QR code; scan it with the iPhone camera
    python pair.py "My iPhone" --url http://192.0.2.10:8765
    python pair.py --list
    python pair.py --remove <id>

Each phone gets its own random 256-bit key. The key is shown once, as the QR code, and is kept only in
keys.json on this PC (and in the phone's keychain). Removing a phone stops it working immediately.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(os.environ.get("FIXER_HOME", r"C:\FixerDesktop"))
KEYS = ROOT / "keys.json"
PORT = int(os.environ.get("FIXER_PORT", "8765"))


def load() -> dict:
    if KEYS.exists():
        return json.loads(KEYS.read_text(encoding="utf-8"))
    return {"phones": {}}


def save(data: dict):
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = KEYS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, KEYS)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))          # no packet is sent; picks the LAN interface
        return s.getsockname()[0]
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?")
    ap.add_argument("--url", help="address the phone uses at home (default: this PC's LAN address)")
    ap.add_argument("--remote", help="address the phone uses away from home (default: away_url in local.json)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--remove")
    a = ap.parse_args()
    data = load()

    if a.list:
        for pid, p in data["phones"].items():
            print(f"{pid}  {p['name']}")
        return
    if a.remove:
        if data["phones"].pop(a.remove, None) is None:
            sys.exit(f"No phone with id {a.remove}")
        save(data)
        print(f"Removed {a.remove}. That phone can no longer use this PC.")
        return
    if not a.name:
        ap.error("give the phone a name, or use --list / --remove")

    pid = secrets.token_hex(6)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    data["phones"][pid] = {"name": a.name, "key": key}
    save(data)
    q = {"u": a.url or f"http://{lan_ip()}:{PORT}", "id": pid, "k": key, "n": a.name}
    try:
        local = json.loads((ROOT / "local.json").read_text(encoding="utf-8"))
    except Exception:
        local = {}
    remote = a.remote or os.environ.get("FIXER_AWAY_URL") or local.get("away_url")
    if remote:
        q["r"] = remote                                # away from home: Cloudflare tunnel via the NAS
    link = "fixer://pair?" + urllib.parse.urlencode(q)

    import qrcode
    qr = qrcode.QRCode(border=2)
    qr.add_data(link)
    qr.print_ascii(invert=True)
    print(f"\nPaired \"{a.name}\" as {pid}. Scan the code with the iPhone camera and tap the Fixer banner.")
    print("The code holds this phone's key: don't share a photo of it.")


if __name__ == "__main__":
    main()
