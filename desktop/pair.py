"""Pair a phone with Fixer Desktop, list paired phones, or remove one.

    python pair.py "the owner's iPhone"            new phone: shows a QR code; scan it with the iPhone camera
    python pair.py "My iPhone" --url http://192.0.2.10:8765
    python pair.py --window                        the tray's "Pair a phone…": the QR code in a window (no console)
    python pair.py --list
    python pair.py --remove <id>
    python pair.py --reset keys                    keys.json can't be read: move it aside (kept, not deleted), start fresh
    python pair.py --reset library                 the same for the add-on list (library.json)

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

import fixer_secure as sec   # DPAPI at-rest sealing — a new pairing writes keys.json sealed, not plaintext

ROOT = Path(os.environ.get("FIXER_HOME", r"C:\FixerDesktop"))
KEYS = ROOT / "keys.json"
PORT = int(os.environ.get("FIXER_PORT", "8765"))


_WARNED = False
LIBRARY = ROOT / ".fixer-private" / "library.json"
RESET_HINT = ("To start over, run: python pair.py --reset keys. That renames keys.json (it is kept, not deleted) "
              "and every phone then needs a new pairing code.")


def reset_config(which: str) -> str:
    """The way out of a keys.json (or library.json) that can't be unsealed, such as after a Windows password reset:
    sec.set_aside renames it to <name>.unreadable-<date>-<time> beside itself, only if it still can't be read, and
    never deletes it. Returns what to tell the owner. Raises sec.LockTimeout if another Fixer process holds it."""
    path = {"keys": KEYS, "library": LIBRARY}[which]
    moved = sec.set_aside(path)
    if moved is None:
        if path.exists():
            return f"{path.name} can be read, so nothing was moved."
        return f"There is no {path.name}, so there is nothing to reset."
    where = f"Moved {path.name} aside as {moved.name} (same folder; nothing was deleted). "
    if which == "keys":
        return where + "Pair each phone again: python pair.py \"phone name\", or Pair a phone in the tray."
    return where + "The add-on list starts empty: add your add-ons again from the phone (the add-on files themselves were not touched)."


def load(strict: bool = False) -> dict:
    """keys.json as {"phones": {...}}, sealed or legacy plaintext. A file that can't be read or parsed counts as no
    phones (warned about once on stderr), so a read never crashes; strict=True raises sec.ConfigUnreadable instead.
    Never save() what load() returned without update(): a read that failed must not become an empty file."""
    global _WARNED
    try:
        data = sec.load_json_object(KEYS)
        if not isinstance(data.get("phones", {}), dict):
            raise sec.ConfigUnreadable("keys.json isn't in the expected form, so it was left unchanged.")
    except sec.ConfigUnreadable as e:
        if strict:
            raise
        if not _WARNED:
            _WARNED = True
            print(f"Warning: {e} No phones are listed until it is fixed or reset. {RESET_HINT}", file=sys.stderr)
        return {"phones": {}}
    data.setdefault("phones", {})
    return data


def update(mutate) -> bool:
    """Every change to keys.json goes through here: under the lock shared with the server (a lock file beside
    keys.json), from a fresh read, written sealed. mutate(data) edits {"phones": {...}} in place and may return False
    for "nothing to change". Raises (and writes nothing) if the current file can't be read or the lock stays busy."""
    def wrapped(data: dict):
        if not isinstance(data.setdefault("phones", {}), dict):
            raise sec.ConfigUnreadable("keys.json isn't in the expected form, so it was left unchanged.")
        return mutate(data)
    return sec.update_json(KEYS, wrapped)


PAIR_WINDOW_S = 60          # 11-19: a new code must be used within this; the server confirms it on first use


def save(data: dict):
    """Whole-file write (sealed, atomic). Not for changing keys.json: use update(), which locks and re-reads."""
    sec.write_text(KEYS, json.dumps(data, indent=2))


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))          # no packet is sent; picks the LAN interface
        return s.getsockname()[0]
    finally:
        s.close()


def new_pairing(name: str, url: str | None = None, remote: str | None = None) -> tuple[str, str, dict]:
    """Adds a phone with a fresh key and returns (id, the fixer://pair link, the link's fields)."""
    import time
    pid = secrets.token_hex(6)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    entry = {"name": name, "key": key, "pair_by": int(time.time()) + PAIR_WINDOW_S}
    update(lambda data: data["phones"].__setitem__(pid, entry))
    q = {"u": url or f"http://{lan_ip()}:{PORT}", "id": pid, "k": key, "n": name}
    try:
        local = json.loads((ROOT / "local.json").read_text(encoding="utf-8"))
    except Exception:
        local = {}
    remote = remote or os.environ.get("FIXER_AWAY_URL") or local.get("away_url")
    if remote:
        q["r"] = remote                                # away from home: Cloudflare tunnel via the NAS
    return pid, "fixer://pair?" + urllib.parse.urlencode(q), q


def show_window(name: str):
    """11-19: the tray's "Pair a phone…". It used to open a console running `cmd /k` with a pre-quoted command,
    which cmd read as one mangled name ("is not recognized as an internal or external command"), and pair.py
    then needed a name the tray never gave. Now: a small window with the QR code as an image (nothing written to
    disk but keys.json), the addresses it carries, and a Close button. Errors show in the window too."""
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.title("Pair a phone — Fixer Desktop")
    root.resizable(False, False)
    pid = None
    try:
        import qrcode
        from PIL import ImageTk
        try:
            pid, link, q = new_pairing(name)
        except sec.ConfigUnreadable as e:              # keys.json can't be unsealed: offer the way out instead of a dead end
            if not messagebox.askyesno("Pair a phone", f"{e}\n\nMove keys.json aside and start fresh? Every phone will need "
                                       "a new pairing code. The old file is kept (renamed), not deleted.", parent=root):
                raise
            messagebox.showinfo("Pair a phone", reset_config("keys"), parent=root)
            pid, link, q = new_pairing(name)
        qr = qrcode.QRCode(border=3, box_size=7)
        qr.add_data(link)
        img = ImageTk.PhotoImage(qr.make_image(fill_color="black", back_color="white").get_image())
    except Exception as e:                             # noqa: BLE001 - shown to the owner, not swallowed
        if pid:                                        # no code was shown: the key it saved must not stay valid
            try:
                update(lambda data: data["phones"].pop(pid, None))
            except Exception:                          # noqa: BLE001
                pass
        root.withdraw()
        messagebox.showerror("Pair a phone", f"Couldn't make a pairing code:\n{e}")
        root.destroy()
        return
    import time
    deadline = time.time() + PAIR_WINDOW_S
    pad = {"padx": 18}
    head = tk.Label(root, text="Scan with the iPhone camera, tap the Fixer banner, then Connect.",
                    font=("Segoe UI", 11, "bold"))
    head.pack(pady=(16, 6), **pad)
    code = tk.Label(root, image=img)
    code.pack(**pad)
    clock = tk.Label(root, text="", font=("Segoe UI", 10))
    clock.pack(pady=(6, 0), **pad)
    where = f"At home: {q['u']}\nAway: {q['r']}" if q.get("r") else (
        f"At home: {q['u']}\nAway: not set up (away_url in local.json), so this phone pairs for home only")
    tk.Label(root, text=where, font=("Segoe UI", 9), justify="center").pack(pady=(6, 2), **pad)
    tk.Label(root, text=f"Saved as \"{name}\" ({pid}). The code is this phone's key: don't share a photo of it.",
             font=("Segoe UI", 9), fg="#666", wraplength=420, justify="center").pack(pady=(2, 8), **pad)
    tk.Button(root, text="Close", width=12, command=root.destroy).pack(pady=(0, 16))
    root.bind("<Escape>", lambda _e: root.destroy())

    def tick():
        """11-19: the code lasts PAIR_WINDOW_S. The server removes "pair_by" when the phone first connects (paired);
        a code still unused at the deadline is deleted here (the server also drops it within 30 s)."""
        try:
            entry = load(strict=True)["phones"].get(pid)
        except Exception:                              # noqa: BLE001 - a half-written file: look again next second
            entry = {"pair_by": deadline}
        if entry is None:                              # the server already dropped it: expired
            head.config(text="This code expired.")
            code.config(image="")
            clock.config(text="Use \"Pair a phone…\" again for a new one.", fg="#b42318")
            return
        if "pair_by" not in entry:
            head.config(text=f"Paired: \"{name}\" is connected.")
            code.config(image="")                      # the key is no longer on screen
            clock.config(text="You can close this window.", fg="#1a7f37")
            root.after(4000, root.destroy)
            return
        left = int(deadline - time.time())
        if left <= 0:
            def expire(data: dict):
                if "pair_by" not in (data["phones"].get(pid) or {}):
                    return False                       # the server confirmed it meanwhile: keep it
                data["phones"].pop(pid)

            try:
                update(expire)
            except Exception:                          # noqa: BLE001
                pass
            head.config(text="This code expired.")
            code.config(image="")
            clock.config(text="Use \"Pair a phone…\" again for a new one.", fg="#b42318")
            return
        clock.config(text=f"Expires in {left} s", fg="#b42318" if left <= 10 else "#333")
        root.after(1000, tick)

    tick()
    root.attributes("-topmost", True)                  # it opens from the tray: make sure it isn't hidden behind
    root.after(1500, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?")
    ap.add_argument("--url", help="address the phone uses at home (default: this PC's LAN address)")
    ap.add_argument("--remote", help="address the phone uses away from home (default: away_url in local.json)")
    ap.add_argument("--window", action="store_true", help="show the QR code in a window (the tray uses this)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--remove")
    ap.add_argument("--reset", choices=["keys", "library"],
                    help="move an unreadable keys.json (or library.json) aside, kept under a new name, and start fresh")
    a = ap.parse_args()

    if a.reset:
        try:
            print(reset_config(a.reset))
        except (OSError, sec.LockTimeout) as e:
            sys.exit(f"Couldn't move it aside: {e}")
        return

    if a.list:
        for pid, p in load()["phones"].items():
            print(f"{pid}  {p['name']}")
        return
    if a.remove:
        found = []

        def remove(data: dict):
            if data["phones"].pop(a.remove, None) is None:
                return False
            found.append(1)

        try:
            update(remove)
        except sec.ConfigUnreadable as e:
            sys.exit(f"{e} {RESET_HINT}")
        except sec.LockTimeout as e:
            sys.exit(str(e))
        if not found:
            sys.exit(f"No phone with id {a.remove}")
        print(f"Removed {a.remove}. That phone can no longer use this PC.")
        return
    if a.window:
        import time
        show_window(a.name or "iPhone " + time.strftime("%Y-%m-%d"))
        return
    if not a.name:
        ap.error("give the phone a name, or use --window / --list / --remove")

    try:
        pid, link, _q = new_pairing(a.name, a.url, a.remote)
    except sec.ConfigUnreadable as e:
        sys.exit(f"{e} {RESET_HINT}")
    except sec.LockTimeout as e:
        sys.exit(str(e))
    import qrcode
    qr = qrcode.QRCode(border=2)
    qr.add_data(link)
    qr.print_ascii(invert=True)
    print(f"\nPaired \"{a.name}\" as {pid}. Scan the code with the iPhone camera and tap the Fixer banner.")
    print("The code holds this phone's key: don't share a photo of it.")


if __name__ == "__main__":
    main()
