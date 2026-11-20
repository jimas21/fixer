"""Speaks the phone's protocol to a running Fixer Desktop, for testing from the PC itself.

    python client_test.py status
    python client_test.py chat "Describe this photo" --image some.jpg
    python client_test.py make "a red fox in snow, photo" --out fox.png   (nothing is written without --out)
    python client_test.py edit "make it night" --image some.jpg
    python client_test.py video "the dog runs" --image some.jpg --out clip.mp4   (no --image: from the words)
    python client_test.py song "upbeat summer pop about a road trip" --seconds 60 --out song.mp3

Uses a throwaway test phone that it adds to keys.json and removes again when it finishes.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import struct
import sys
import time
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).parent))
import pair  # noqa: E402


def call(url: str, phone: str, key: bytes, op: str, payload: dict, stamp: int | None = None):
    stamp = stamp or int(time.time())
    aad = f"fixer1|{op}|{phone}|{stamp}"
    nonce = os.urandom(12)
    body = nonce + AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), aad.encode())
    req = urllib.request.Request(f"{url}/v1/{op}", data=body, method="POST",
                                 headers={"X-Fixer-Phone": phone, "X-Fixer-Time": str(stamp),
                                          # Cloudflare (the away address) blocks Python's default User-Agent: 1010
                                          "User-Agent": "FixerTest/1"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        seq = 0
        while True:
            head = r.read(4)
            if len(head) < 4:
                return
            n = struct.unpack(">I", head)[0]
            frame = r.read(n)
            obj = json.loads(AESGCM(key).decrypt(frame[:12], frame[12:], f"{aad}|{seq}".encode()))
            seq += 1
            yield obj
            if obj["t"] in ("done", "error"):
                return


class TestPhone:
    def __enter__(self):
        self.id = "test-" + secrets.token_hex(4)
        self.key = secrets.token_bytes(32)
        d = pair.load()
        d["phones"][self.id] = {"name": "client_test", "key": base64.urlsafe_b64encode(self.key).decode().rstrip("=")}
        pair.save(d)
        time.sleep(0.05)
        return self

    def __exit__(self, *a):
        d = pair.load()
        d["phones"].pop(self.id, None)
        pair.save(d)


def b64file(p: str) -> str:
    return base64.b64encode(Path(p).read_bytes()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("op", choices=["status", "chat", "make", "edit", "video", "song"])
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--image")
    ap.add_argument("--model")
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--lyrics", default="")
    ap.add_argument("--instrumental", action="store_true")
    ap.add_argument("--out", help="save the image here (default: nothing is written)")
    ap.add_argument("--url", default=f"http://127.0.0.1:{pair.PORT}")
    a = ap.parse_args()

    if a.op == "status":
        op, payload = "status", {}
    elif a.op == "chat":
        m = {"role": "user", "text": a.text}
        if a.image:
            m["image"] = b64file(a.image)
        op, payload = "chat", {"messages": [m], "model": a.model, "temperature": 0.6, "max_tokens": 400}
    elif a.op == "video":
        op, payload = "video", {"prompt": a.text, "model": a.model, "seconds": 5}
        if a.image:
            payload["image"] = b64file(a.image)
    elif a.op == "song":
        op, payload = "song", {"prompt": a.text, "seconds": a.seconds, "lyrics": a.lyrics,
                               "instrumental": a.instrumental}
    else:
        op, payload = "image", {"prompt": a.text, "model": a.model, "steps": a.steps, "guidance": 1.0}
        if a.op == "edit":
            payload["image"] = b64file(a.image)

    t0 = time.time()
    with TestPhone() as ph:
        for f in call(a.url, ph.id, ph.key, op, payload):
            if f["t"] == "text":
                print(f["v"], end="", flush=True)
            elif f["t"] == "image":
                data = base64.b64decode(f["v"])
                print(f"\n[image {len(data) // 1024} KB, seed {f.get('seed')}]")
                if a.out:
                    Path(a.out).write_bytes(data)
            elif f["t"] == "video":
                data = base64.b64decode(f["v"])
                print(f"\n[video {len(data) // 1024} KB, {f.get('width')}x{f.get('height')}, {f.get('seconds')} s, seed {f.get('seed')}]")
                if a.out:
                    Path(a.out).write_bytes(data)
            elif f["t"] == "audio":
                data = base64.b64decode(f["v"])
                meta = {k: v for k, v in f.items() if k not in ("t", "v")}
                print(f"\n[audio {len(data) // 1024} KB] {json.dumps(meta)}")
                if a.out:
                    Path(a.out).write_bytes(data)
            elif f["t"] == "progress":
                print(f"\r{int(f['v'] * 100)}%", end="", flush=True)
            else:
                print(f"\n[{f['t']}] {json.dumps({k: v for k, v in f.items() if k != 't'})}")
    print(f"({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
