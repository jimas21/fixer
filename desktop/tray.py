"""Fixer Desktop tray icon: the owner's on/off switch.

Green = on, amber = working, grey = off. Hover for the state. Right-click:
  Turn on / Turn off          off stops the server entirely (asks it to quit itself, forces it only if it will not;
                              ComfyUI and the chat model go with it, all graphics memory and RAM returned)
  Free graphics memory        unloads the models but keeps the server on (they reload on the next request)
  Start with Windows          puts this tray icon in the Startup folder
  Turn on automatically       whether the server starts when the tray starts
  Pair a phone…               opens a window with the QR code
  Open folder
  Quit                        stops the server the same way, then removes the icon (11-18: if not everything stopped
                              it tries once more first; Turn off then shows "Couldn't stop everything", not "Off";
                              a Quit still short after that shows a notification and keeps the icon ~5 s first)

Only one tray can run (a second one exits at once). The tray owns the server: it puts it in a job object, so if the
tray is ended in any way (Task Manager, a closing session) the server and its children end too. To control the install
from a terminal use fixerctl.py (status / stop / start / restart / deploy), never hand kills.

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

import fixerctl as fx           # beside this file, in the install and in the repo's desktop folder

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "Scripts" / "python.exe"
SERVER = ROOT / "fixer_desktop.py"
CONF = ROOT / "tray.json"
PORT = int(os.environ.get("FIXER_PORT", "8765"))
LOG = ROOT / "tray.log"
STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Fixer Desktop.lnk"
NO_WINDOW = 0x08000000
STOP_FAILED = "Couldn't stop everything (see tray.log)"   # 11-18: shown instead of "Off" when a stop fell short
QUIT_NOTICE = ("Fixer Desktop couldn't stop everything before quitting. Some of it may still be running: "
               "see tray.log in its folder.")
QUIT_NOTICE_S = 5.0                                     # 11-18: a short Quit keeps the icon this long so it is seen
_sleep = time.sleep                                     # the checks swap this out


def load_conf() -> dict:
    try:
        return json.loads(CONF.read_text(encoding="utf-8"))
    except Exception:
        return {"on_at_login": True}


def save_conf(c: dict):
    CONF.write_text(json.dumps(c, indent=2), encoding="utf-8")


def log(msg: str):
    """One short line per stop (no request data). Never raises; the file is cut when it grows past 200 KB."""
    try:
        if LOG.exists() and LOG.stat().st_size > 200_000:
            LOG.write_text("", encoding="utf-8")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def icon_image(state: str) -> Image.Image:
    color = {"off": (130, 130, 140), "idle": (60, 190, 110), "loaded": (60, 190, 110), "busy": (250, 190, 60),
             "starting": (90, 150, 230), "stopping": (225, 120, 90)}[state]
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(45, 35, 95, 255))
    d.line((14, 50, 40, 24), fill=(240, 240, 255, 255), width=7)
    d.ellipse((38, 8, 58, 28), fill=color + (255,))
    return img


CREATE_SUSPENDED = 0x00000004
SERVER_LOG = ROOT / "server.log"


def server_log():
    """11-20: the server's stderr (its warnings and any crash traceback; never request data) goes to server.log,
    started fresh once it passes 1 MB. Before this it went nowhere, so a server that stopped serving left no cause."""
    try:
        if SERVER_LOG.exists() and SERVER_LOG.stat().st_size > 1_000_000:
            SERVER_LOG.write_text("", encoding="utf-8")
        f = open(SERVER_LOG, "a", encoding="utf-8")
        f.write(time.strftime("--- %Y-%m-%d %H:%M:%S server start\n"))
        f.flush()
        return f
    except Exception:                                   # noqa: BLE001
        return subprocess.DEVNULL


def start_in_job(cmd: list[str], cwd, job) -> subprocess.Popen:
    """11-18: starts cmd suspended, puts it in `job`, then lets it run, so nothing it starts (the venv launcher's real
    Python, the server's children) can be born outside the job. If it cannot be resumed it is killed and started the
    old way (start, then add), and that is logged."""
    io = dict(stdout=subprocess.DEVNULL, stderr=server_log(), stdin=subprocess.DEVNULL)
    p = subprocess.Popen(cmd, cwd=str(cwd), creationflags=NO_WINDOW | CREATE_SUSPENDED, **io)
    added = job.add(p)
    if fx.resume_process(p):
        if not added:
            log("server not placed in the tray's job (carrying on without it)")
        return p
    log("could not resume the suspended server; starting it the old way")
    try:
        p.kill()
        p.wait(10)
    except Exception:                                   # noqa: BLE001
        pass
    p = subprocess.Popen(cmd, cwd=str(cwd), creationflags=NO_WINDOW, **io)
    if not job.add(p):
        log("server not placed in the tray's job (carrying on without it)")
    return p


class Tray:
    def __init__(self):
        self.conf = load_conf()
        self.proc: subprocess.Popen | None = None
        self.state = "off"
        self.detail = "Off"
        self.job = fx.Job()                             # the server lives in this: it ends when this tray ends
        self.stopping = False
        self.stop_failed = False                        # 11-18: the last stop fell short (shown instead of "Off")
        self.want_quit = False                          # 11-18: set and read only under quit_lock
        self.stop_running = False                       # a stop worker owns the stop (under quit_lock)
        self.quitting = False                           # the icon is about to go (after a Quit's stop)
        self.quit_lock = threading.Lock()
        self.stop_thread: threading.Thread | None = None
        self.icon = pystray.Icon("fixer", icon_image("off"), "Fixer Desktop — Off", menu=self.menu())

    # ---- server process

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def turn_on(self, *_):
        with self.quit_lock:                            # 11-18: read together with the stop worker's last step
            if self.stopping or self.quitting:
                return
        if self.running():
            return
        if self.reachable():                            # already serving (started by hand or by an older tray): adopt it
            return
        self.job.close()                                # a fresh job each time; closing the old one clears strays
        self.job = fx.Job()
        self.stop_failed = False
        self.proc = start_in_job([str(PY), "-u", str(SERVER)], ROOT, self.job)
        self.set("starting", "Starting…")

    def stop_all(self, quit_after: bool = False):
        """Stops the server, ComfyUI and the chat model, off the menu thread; then (Quit) removes the icon."""
        # 11-18: want_quit and stop_running change together under quit_lock, and the worker reads want_quit under it
        # in the same step that ends its run: a Quit clicked while a stop is finishing is either seen by that worker
        # or finds no worker and starts its own. It can no longer fall between the two.
        with self.quit_lock:
            if self.quitting:
                return                                  # 11-18: the icon is already going (e.g. the 5 s notice)
            if quit_after:
                self.want_quit = True
            if self.stop_running:
                return                                  # already stopping; it sees the Quit before it finishes
            self.stop_running = True
            self.stopping = True
        try:
            self.stop_thread = threading.Thread(target=self._stop_worker, daemon=False, name="tray-stop")
            self.stop_thread.start()
        except BaseException:
            with self.quit_lock:
                self.stop_running = self.stopping = False
            raise

    def _stop_worker(self):
        ok, retried, quit_now, ended = False, False, False, False
        try:
            self.set("stopping", "Stopping…")
            ok = self._stop_once()
            while True:
                with self.quit_lock:
                    quit_now = self.want_quit
                    if ok or not quit_now or retried:
                        self.stop_failed = not ok
                        self.proc = None
                        # 11-18: set in the same step that clears "stopping", so Turn on can never start a
                        # server in between
                        self.quitting = self.quitting or quit_now
                        self.stopping = False
                        self.stop_running = False
                        ended = True
                        break
                log("not everything stopped; trying once more before quitting")   # Quit tries once more first
                retried = True
                ok = self._stop_once()
        finally:
            if not ended:                               # an unexpected error: never leave the tray stuck 'Stopping'
                with self.quit_lock:
                    self.stop_failed = not ok
                    quit_now = self.want_quit
                    self.quitting = self.quitting or quit_now
                    self.stopping = False
                    self.stop_running = False
        self.set("off", "Off" if ok else STOP_FAILED)
        if quit_now:                                    # quitting is already set: Turn on and Quit are ignored now
            if not ok:
                self._show_quit_failure()
            self.icon.stop()

    def _show_quit_failure(self):
        """11-18: a Quit that fell short: a Windows notification (when this pystray has one), and the icon stays with
        its "Couldn't stop everything" title for QUIT_NOTICE_S seconds. Nothing here can stop the quit."""
        log("quit fell short; showing it before the icon goes")
        try:
            notify = getattr(self.icon, "notify", None)
            if callable(notify) and getattr(self.icon, "HAS_NOTIFICATION", True):
                notify(QUIT_NOTICE, "Fixer Desktop")
        except Exception as ex:                         # noqa: BLE001
            log(f"notification failed: {type(ex).__name__}")
        try:
            _sleep(QUIT_NOTICE_S)
        except Exception:                               # noqa: BLE001
            pass

    def _stop_once(self) -> bool:
        """One run of the shared stop routine. True only when it reports everything stopped."""
        try:
            rep = fx.stop_everything(ROOT, PORT, proc=self.proc, job=self.job,
                                     protect={os.getpid(), os.getppid()}, say=log)
            log(f"stopped: polite={rep['polite']} forced={rep['forced']} swept={rep['swept']} "
                f"port_closed={rep['port_closed']} remaining={rep['remaining']} unkilled={rep.get('unkilled', [])} "
                f"ok={rep['ok']}")
            return bool(rep.get("ok"))
        except Exception as ex:                         # noqa: BLE001
            log(f"stop failed: {type(ex).__name__}: {ex}")
            return False

    def turn_off(self, *_):
        self.stop_all(False)

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
            if self.stopping:
                time.sleep(0.5)
                continue
            s = self.reachable()
            if s is None:
                if self.state == "starting" and self.running():
                    pass
                else:
                    self.set("off", STOP_FAILED if self.stop_failed else "Off")
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
        # 11-19: straight to a QR window (pythonw, no console). The old `cmd /c start ... cmd /k "<quoted>"` reached
        # cmd as \"...\" (Python's argument quoting), which cmd reported as "not recognized", and gave pair.py no name.
        pyw = PY.with_name("pythonw.exe")
        try:
            subprocess.Popen([str(pyw if pyw.exists() else PY), str(ROOT / "pair.py"), "--window"],
                             cwd=str(ROOT), creationflags=NO_WINDOW)
        except Exception as e:                       # noqa: BLE001
            log(f"pair: couldn't start pair.py: {e}")

    def open_folder(self, *_):
        os.startfile(str(ROOT))

    def quit(self, *_):
        self.stop_all(True)                             # the server first, then the icon

    def menu(self):
        on = lambda item: self.state not in ("off", "stopping")  # noqa: E731
        return pystray.Menu(
            pystray.MenuItem(lambda item: f"Fixer Desktop — {self.detail}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Turn on", self.turn_on, visible=lambda i: self.state == "off", default=True),
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
    _held = fx.acquire_single()                         # first thing: one tray only
    if _held is None:
        sys.exit(0)
    Tray().run()
