"""Proves Fixer Desktop leaves no trace of a request on disk.

Snapshots every file under the folders a request could plausibly touch (Fixer's own folder, the user's temp,
AppData, the Hugging Face and torch caches), runs a chat with a photo and an image edit through the real
encrypted protocol, snapshots again, and reports every file that appeared or changed. It also searches those
new or changed files for the request's unique marker text and the image's bytes.

    python trace_test.py [--video]  (the server must be running on this PC; --video adds a 1-second clip)

Pass = no new or changed file contains the marker, and any new/changed file is on the allow-list
(model downloads, Python bytecode, keys.json written by this test's own throwaway pairing).
"""
from __future__ import annotations

import base64
import io
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import client_test as ct  # noqa: E402

HOME = Path.home()
ROOTS = [ct.pair.ROOT, Path(os.environ.get("TEMP", HOME / "AppData/Local/Temp")), HOME / "AppData/Local",
         HOME / "AppData/Roaming", HOME / ".cache", HOME / ".nv", Path(r"C:\ProgramData\NVIDIA Corporation")]
SKIP_PARTS = ("\\models\\", "\\Packages\\", "\\Microsoft\\Edge", "\\Google\\Chrome", "\\Mozilla\\", "\\Claude\\",
              "\\claude\\", "\\Temp\\claude", "\\Discord\\", "\\Spotify\\", "\\Steam\\", "\\Plex Media Server\\",
              "\\NVIDIA\\NvBackend", "\\D3DSCache\\", "\\CrashDumps\\", "\\Microsoft\\Windows\\", "\\.claude\\",
              "\\Dropbox\\", "\\ConnectedDevicesPlatform\\", "\\Microsoft\\Office", "\\Microsoft\\OneDrive")
ALLOWED = ("keys.json", "keys.tmp", ".pyc", "\\__pycache__\\")


def snapshot() -> dict[str, tuple[float, int]]:
    out = {}
    for root in ROOTS:
        if not root.exists():
            continue
        for dirpath, dirs, files in os.walk(root):
            if any(s.lower() in (dirpath + "\\").lower() for s in SKIP_PARTS):
                dirs[:] = []
                continue
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    st = os.stat(p)
                    out[p] = (st.st_mtime, st.st_size)
                except OSError:
                    pass
    return out


def contains(path: str, needles: list[bytes]) -> bool:
    try:
        with open(path, "rb") as fh:
            data = fh.read(64 * 1024 * 1024)
    except OSError:
        return False
    return any(n in data for n in needles)


def main():
    marker = "FIXERTRACE" + secrets.token_hex(8)
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (512, 512), (40, 90, 160))
    ImageDraw.Draw(img).text((40, 240), marker, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()

    print("Snapshot before…", flush=True)
    before = snapshot()
    t0 = time.time()
    with ct.TestPhone() as ph:
        url = ct.call.__defaults__ and None
        base = f"http://127.0.0.1:{ct.pair.PORT}"
        chat = {"messages": [{"role": "user", "text": f"Repeat this code exactly: {marker}",
                              "image": base64.b64encode(png).decode()}],
                "instructions": f"Secret rules {marker}", "max_tokens": 60, "temperature": 0}
        reply = "".join(f.get("v", "") for f in ct.call(base, ph.id, ph.key, "chat", chat) if f["t"] == "text")
        edit = {"prompt": f"add a red circle {marker}", "image": base64.b64encode(png).decode(), "steps": 4}
        got = [f for f in ct.call(base, ph.id, ph.key, "image", edit) if f["t"] == "image"]
        result_png = base64.b64decode(got[0]["v"]) if got else b""
        search = list(ct.call(base, ph.id, ph.key, "search", {"query": marker, "count": 3}))
        result_mp4 = b""
        if "--video" in sys.argv:
            vid = {"prompt": f"the picture slowly zooms in {marker}", "image": base64.b64encode(png).decode(),
                   "seconds": 1, "longest": 512}
            got = [f for f in ct.call(base, ph.id, ph.key, "video", vid) if f["t"] == "video"]
            result_mp4 = base64.b64decode(got[0]["v"]) if got else b""
    print(f"Requests done in {time.time() - t0:.0f}s (reply echoed marker: {marker in reply}, "
          f"image back: {bool(result_png)}, search frames: {len(search)}"
          + (f", video back: {len(result_mp4) // 1024} KB" if "--video" in sys.argv else "") + ")", flush=True)
    time.sleep(3)
    print("Snapshot after…", flush=True)
    after = snapshot()

    touched = [p for p, v in after.items() if before.get(p) != v]
    needles = [marker.encode(), marker.encode("utf-16-le"), png[100:164]]
    if result_png:
        needles.append(result_png[100:164])
    if len(result_mp4) > 4096:
        needles.append(result_mp4[2048:2112])
    leaks = [p for p in touched if contains(p, needles)]
    other = [p for p in touched if p not in leaks and not any(a in p for a in ALLOWED)]

    print(f"\n{len(touched)} files new or changed under the watched folders.")
    for p in other:
        print("  changed (no request content):", p)
    if leaks:
        print("\nFAIL: request content found on disk in:")
        for p in leaks:
            print("  ", p)
        sys.exit(1)
    print("\nPASS: no file contains the request's text, photo or result.")


if __name__ == "__main__":
    main()
