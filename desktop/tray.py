"""Fixer Desktop tray icon: the owner's on/off switch.

Green = on, amber = working, grey = off. Hover for the state. Right-click:
  Turn on / Turn off          off stops the server entirely (all graphics memory and RAM returned)
  Free graphics memory        unloads the models but keeps the server on (they reload on the next request)
  Start with Windows          puts this tray icon in the Startup folder
  Turn on automatically       whether the server starts when the tray starts
  Pair a phone…               opens a window with the QR code
  Open folder / Quit

Settings for the tray itself live in tray.json beside it (no request data).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "Scripts" / "python.exe"
SERVER = ROOT / "fixer_desktop.py"
CONF = ROOT / "tray.json"
PORT = int(os.environ.get("FIXER_PORT", "8765"))
STARTUP = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Fixer Desktop.lnk"
NO_WINDOW = 0x08000000


def load_conf() -> dict:
    try:
        return json.loads(CONF.read_text(encoding="utf-8"))
    except Exception:
        return {"on_at_login": True}


def save_conf(c: dict):
    CONF.write_text(json.dumps(c, indent=2), encoding="utf-8")


def icon_image(state: str) -> Image.Image:
    color = {"off": (130, 130, 140), "idle": (60, 190, 110), "loaded": (60, 190, 110), "busy": (250, 190, 60),
             "starting": (90, 150, 230)}[state]
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(45, 35, 95, 255))
    d.line((14, 50, 40, 24), fill=(240, 240, 255, 255), width=7)
    d.ellipse((38, 8, 58, 28), fill=color + (255,))
    return img


class Tray:
    def __init__(self):
        self.conf = load_conf()
        self.proc: subprocess.Popen | None = None
        self.state = "off"
        self.detail = "Off"
        self.icon = pystray.Icon("fixer", icon_image("off"), "Fixer Desktop — Off", menu=self.menu())

    # ---- server process

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def turn_on(self, *_):
        if self.running() or self.reachable():
            return
        self.proc = subprocess.Popen([str(PY), "-u", str(SERVER)], cwd=str(ROOT), creationflags=NO_WINDOW,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        self.set("starting", "Starting…")

    def turn_off(self, *_):
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], creationflags=NO_WINDOW,
                           capture_output=True)
        else:
            # Started some other way (e.g. by hand): stop whatever listens on the port.
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue).OwningProcess"],
                                 capture_output=True, text=True, creationflags=NO_WINDOW).stdout.split()
            for pid in out:
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], creationflags=NO_WINDOW, capture_output=True)
        self.proc = None
        self.set("off", "Off")

    def free_gpu(self, *_):
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/local/unload", method="POST"),
                                   timeout=60).read()
        except Exception:
            pass

    def reachable(self) -> dict | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/local/state", timeout=2) as r:
                return json.loads(r.read())
        except Exception:
            return None

    # ---- display

    def set(self, state: str, detail: str):
        if (state, detail) == (self.state, self.detail):
            return
        self.state, self.detail = state, detail
        self.icon.icon = icon_image(state)
        self.icon.title = f"Fixer Desktop — {detail}"[:127]
        self.icon.update_menu()

    def watch(self):
        while True:
            s = self.reachable()
            if s is None:
                if self.state == "starting" and self.running():
                    pass
                else:
                    self.set("off", "Off")
            elif s.get("busy"):
                self.set("busy", "Working…")
            elif s.get("loaded"):
                self.set("loaded", "On, ready")
            else:
                self.set("idle", "On, ready")
            time.sleep(2)

    # ---- startup / pairing

    def startup_enabled(self) -> bool:
        return STARTUP.exists()

    def toggle_startup(self, *_):
        if STARTUP.exists():
            STARTUP.unlink()
        else:
            pyw = ROOT / "venv" / "Scripts" / "pythonw.exe"
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');$s.TargetPath='{t}';"
                  "$s.Arguments='\"{a}\"';$s.WorkingDirectory='{w}';$s.Description='Fixer Desktop tray';$s.Save()"
                  ).format(lnk=STARTUP, t=pyw, a=Path(__file__).resolve(), w=ROOT)
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=NO_WINDOW, capture_output=True)
        self.icon.update_menu()

    def toggle_on_at_login(self, *_):
        self.conf["on_at_login"] = not self.conf.get("on_at_login", True)
        save_conf(self.conf)
        self.icon.update_menu()

    def pair(self, *_):
        subprocess.Popen(["cmd", "/c", "start", "Pair a phone", "cmd", "/k",
                          f'"{PY}" "{ROOT / "pair.py"}"'], cwd=str(ROOT))

    def open_folder(self, *_):
        os.startfile(str(ROOT))

    def quit(self, *_):
        self.icon.stop()

    def menu(self):
        on = lambda item: self.state != "off"  # noqa: E731
        return pystray.Menu(
            pystray.MenuItem(lambda item: f"Fixer Desktop — {self.detail}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Turn on", self.turn_on, visible=lambda i: not on(i), default=True),
            pystray.MenuItem("Turn off", self.turn_off, visible=on),
            pystray.MenuItem("Free graphics memory", self.free_gpu, visible=lambda i: self.state == "loaded"),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", self.toggle_startup, checked=lambda i: self.startup_enabled()),
            pystray.MenuItem("Turn on automatically", self.toggle_on_at_login,
                             checked=lambda i: self.conf.get("on_at_login", True)),
            pystray.MenuItem("Pair a phone…", self.pair),
            pystray.MenuItem("Open folder", self.open_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )

    def run(self):
        if self.conf.get("on_at_login", True) and "--no-autostart" not in sys.argv:
            self.turn_on()
        threading.Thread(target=self.watch, daemon=True).start()
        self.icon.run()


if __name__ == "__main__":
    Tray().run()
