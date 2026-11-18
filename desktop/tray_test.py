"""Checks for the tray's stop routine, the leftover rule and the single-instance mutex (11-18). Fakes only: no real
server, no real kills, no tray window, nothing read from or written to the live install.

    python desktop\\tray_test.py        (any Windows Python; pystray and Pillow are stubbed here)
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="fixer_tray_test_")
os.environ["FIXER_HOME"] = TMP                          # nothing is read from or written to the live install
os.environ["FIXER_TRAY_MUTEX"] = r"Local\FixerTrayTest" + str(os.getpid())
os.environ.setdefault("APPDATA", TMP)
sys.path.insert(0, str(Path(__file__).resolve().parent))

# pystray and PIL are stubbed so importing tray.py cannot open or draw anything.
EVENTS: list[str] = []


class FakeIcon:
    def __init__(self, name, image, title, menu=None):
        self.icon, self.title, self.menu = image, title, menu

    def update_menu(self):
        pass

    def stop(self):
        EVENTS.append("icon.stop")

    def run(self):
        pass


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


pystray_stub = types.SimpleNamespace(Icon=FakeIcon, Menu=FakeMenu, MenuItem=lambda *a, **k: (a, k))
sys.modules["pystray"] = pystray_stub
sys.modules["PIL"] = mock.MagicMock()
sys.modules["PIL.Image"] = mock.MagicMock()
sys.modules["PIL.ImageDraw"] = mock.MagicMock()

import fixerctl as fx  # noqa: E402
import tray  # noqa: E402

tray.LOG = Path(TMP) / "tray.log"
SLEPT: list[float] = []
tray._sleep = lambda s: SLEPT.append(s)                 # the 5 s quit notice never really waits here

FAILS: list[str] = []
CHECKS = 0


def check(cond, msg: str):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


# ------------------------------------------------------------------ the leftover rule

R = r"C:\FixerDesktop"
ME = {1000, 999}


def lo(exe, cmd, pid=5000, ppid=1, root=R, protect=ME):
    return fx.is_leftover(pid, exe, cmd, ppid, root, protect)


check(lo(r"C:\FixerDesktop\llama\llama-server.exe", r"C:\FixerDesktop\llama\llama-server.exe -m x.gguf"),
      "llama-server under ROOT is a leftover")
check(lo(r"C:\FixerDesktop\comfyui\.venv\Scripts\python.exe", r"C:\FixerDesktop\comfyui\.venv\Scripts\python.exe -s main.py"),
      "ComfyUI's venv python is a leftover")
check(lo(r"C:\Users\x\AppData\Local\Programs\Python\Python312\python.exe",
         r'"C:\Users\x\Python312\python.exe" -s main.py --temp-directory C:\FixerDesktop\comfy-private\temp'),
      "ComfyUI's base-python child (command line names comfy-private) is a leftover")
# 11-18: ComfyUI started through the host guard (comfy_guard.py beside the server, every other flag unchanged)
GUARD_FLAGS = (r" -s C:\FixerDesktop\comfy_guard.py --listen 127.0.0.1 --port 8188 --disable-auto-launch"
               r" --temp-directory C:\FixerDesktop\comfy-private\temp --input-directory C:\FixerDesktop\comfy-private\input"
               r" --extra-model-paths-config C:\FixerDesktop\comfy-private\paths.yaml --offline")
check(lo(r"C:\FixerDesktop\comfyui\.venv\Scripts\python.exe",
         r'"C:\FixerDesktop\comfyui\.venv\Scripts\python.exe"' + GUARD_FLAGS),
      "the guarded ComfyUI launcher (ROOT\\comfyui\\.venv python running comfy_guard.py) is a leftover")
check(lo(r"C:\Users\x\AppData\Local\Programs\Python\Python312\python.exe",
         r'"C:\Users\x\AppData\Local\Programs\Python\Python312\python.exe"' + GUARD_FLAGS),
      "the guarded ComfyUI's base-python child (names ROOT\\comfy_guard.py and ROOT\\comfy-private) is a leftover")
check(not lo(r"C:\Users\x\AppData\Local\Programs\Python\Python312\python.exe",
             r'"C:\Users\x\Python312\python.exe" -s C:\FixerDesktop2\comfy_guard.py --temp-directory C:\FixerDesktop2\comfy-private\temp'),
      "another install's guarded ComfyUI is not ours")
check(not lo(r"C:\Program Files\Notepad++\notepad++.exe", r"notepad++ C:\FixerDesktop\comfy_guard.py"),
      "an editor with comfy_guard.py open is excluded")
check(lo(r"C:\Users\x\AppData\Local\Programs\Python\Python312\python.exe",
         r'"C:\Users\x\Python312\python.exe" -u C:\FixerDesktop\fixer_desktop.py'),
      "the base-python child running ROOT\\fixer_desktop.py is a leftover")
check(lo(r"C:\FixerDesktop\venv\Scripts\python.exe", r'"C:\FixerDesktop\venv\Scripts\python.exe" -u C:\FixerDesktop\fixer_desktop.py'),
      "the venv launcher of the server is a leftover")
check(lo(r"c:\fixerdesktop\LLAMA\llama-server.exe", "x"), "paths compare without regard to case")
check(lo(r"C:\Python312\python.exe", "python.exe c:/FixerDesktop/fixer_desktop.py"), "forward slashes still match")

check(not lo(r"C:\FixerDesktop\venv\Scripts\python.exe", r"python.exe -c print(1)"),
      "a venv python NOT running the server is left alone")
check(not lo(r"C:\FixerDesktop\venv\Scripts\pythonw.exe", r'"C:\FixerDesktop\venv\Scripts\pythonw.exe" C:\FixerDesktop\tray.py'),
      "this install's tray is excluded")
check(not lo(r"C:\Python312\python.exe", r"python C:\FixerDesktop\tray.py"), "another tray.py is excluded")
check(not lo(r"C:\Python312\python.exe", r"python C:\FixerDesktop\pair.py"), "pair.py is excluded")
check(not lo(r"C:\Python312\python.exe", r"python C:\Users\x\fixer\desktop\fixerctl.py stop"), "fixerctl.py is excluded")
check(not lo(r"C:\FixerDesktop\llama\llama-server.exe", "x", pid=1000), "this process is excluded by pid")
check(not lo(r"C:\FixerDesktop\venv\Scripts\python.exe", "python C:\\FixerDesktop\\fixer_desktop.py", pid=999),
      "its launcher parent is excluded by pid")
check(not lo(r"C:\Python312\python.exe", "python.exe other.py"), "an unrelated python.exe is excluded")
check(not lo(r"C:\Games\Game\game.exe", "game.exe -fullscreen"), "a game is excluded")
check(not lo(r"C:\FixerDesktop2\llama\llama-server.exe", r"C:\FixerDesktop2\llama\llama-server.exe"),
      "C:\\FixerDesktop2 is not under C:\\FixerDesktop (exe)")
check(not lo(r"C:\Python312\python.exe", r"python C:\FixerDesktop2\fixer_desktop.py"),
      "C:\\FixerDesktop2 is not under C:\\FixerDesktop (command line)")
check(not lo(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
             r"powershell -Command Get-Content C:\FixerDesktop\fixer_desktop.py"), "a shell that merely mentions it is excluded")
check(not lo("", ""), "an unreadable process (no path, no command line) is excluded")
check(not lo(r"C:\Program Files\Notepad++\notepad++.exe", r'notepad++ C:\FixerDesktop\fixer_desktop.py'),
      "an editor with the server script in its arguments is excluded")
check(not lo(r"C:\Users\x\Code.exe", r"Code.exe C:\FixerDesktop\comfyui\main.py"), "an editor with a comfyui path is excluded")
check(not fx.is_tray(r"C:\p\python.exe", r"python C:\OtherApp\tray.py", R), "another project's tray.py is not ours")
check(fx.is_tray(r"C:\p\python.exe", r'"C:\p\python.exe" "C:\FixerDesktop\tray.py"', R), "a quoted tray.py under ROOT is ours")
check(fx.is_tray(r"C:\FixerDesktop\venv\Scripts\pythonw.exe", r"pythonw C:\FixerDesktop\tray.py", R)
      and fx.is_tray(r"C:\p\python.exe", "python " + str(fx.HERE / "tray.py")), "ROOT tray and this repo's tray count")
check(fx.is_tray(r"C:\FixerDesktop\venv\Scripts\pythonw.exe", r"pythonw C:\FixerDesktop\tray.py", R)
      and not fx.is_tray(r"C:\x\notepad.exe", r"notepad tray.py") and not fx.is_tray(r"C:\p\python.exe", "python pair.py"),
      "is_tray recognises python running tray.py only")

# ------------------------------------------------------------------ the single-instance mutex

NAME = r"Local\FixerTrayMutexTest" + str(os.getpid())
a = fx.acquire_single(NAME)
check(a, "the first acquire gets the mutex")
check(fx.single_held(NAME), "single_held sees it")
check(fx.acquire_single(NAME) is None, "the second acquire is refused")
fx.release_single(a)
check(not fx.single_held(NAME), "released: no longer held")
c = fx.acquire_single(NAME)
check(c, "a released mutex can be taken again")
fx.release_single(c)

# ------------------------------------------------------------------ stop order, with a fake world


class World:
    """A pretend server. quit_mode: 'ok' (leaves when asked), 'hang' (says ok, stays), 'old' (404), 'down'."""

    def __init__(self, quit_mode="ok", pid=4242, leftovers=(), sticky=False):
        self.up = True
        self.quit_mode, self.pid, self.sticky = quit_mode, pid, sticky
        self.leftovers = [dict(pid=p, ppid=1, name="llama-server.exe", exe="", cmd="") for p in leftovers]
        self.clock = 0.0
        self.owned = True
        self.ev: list[str] = []

    def install(self, with_pid=True):
        w = self
        patches = {
            "state": lambda port=0: ({"busy": False, **({"pid": w.pid} if with_pid else {})} if w.up else None),
            "port_open": lambda port=0: w.up,
            "pid_alive": lambda pid: bool(w.up and pid == w.pid),
            "ask_quit": lambda port=0: w._ask(),
            "kill_tree": lambda pid: w._kill(pid),
            "port_pids": lambda port=0: [w.pid],
            "install_owned": lambda pid, root=None, protect=(): w.owned,
            "find_leftovers": lambda root=None, protect=(), rows=None: list(w.leftovers),
            "_sleep": lambda s: setattr(w, "clock", w.clock + s),
            "_mono": lambda: w.clock,
        }
        return mock.patch.multiple(fx, **patches)

    def _ask(self):
        self.ev.append("ask_quit")
        if self.quit_mode == "ok":
            self.up = False
            return "ok"
        return {"hang": "ok", "old": "old", "down": "down"}[self.quit_mode]

    def _kill(self, pid):
        self.ev.append(f"kill:{pid}")
        if pid == self.pid:
            self.up = False
        self.leftovers = [d for d in self.leftovers if d["pid"] != pid or self.sticky]
        return True


class FakeProc:
    def __init__(self, world, pid=777):
        self.w, self.pid = world, pid

    def poll(self):
        return None if self.w.up else 0


class FakeJob:
    def __init__(self, world):
        self.w = world

    def terminate(self):
        self.w.ev.append("job.terminate")
        self.w.up = False


# polite: asks, never forces
w = World("ok")
with w.install():
    rep = fx.stop_everything(R, 8765, proc=FakeProc(w), job=FakeJob(w), protect=ME)
check(w.ev == ["ask_quit"], f"polite stop only asks (got {w.ev})")
check(rep["polite"] and not rep["forced"] and rep["ok"], "polite stop is reported as polite and ok")

# the server says ok but hangs: forced only after the wait, job first
w = World("hang")
with w.install():
    rep = fx.stop_everything(R, 8765, proc=FakeProc(w), job=FakeJob(w), protect=ME, wait=20)
check(w.ev[0] == "ask_quit" and "job.terminate" in w.ev, f"hung server: asked first, then the job is ended ({w.ev})")
check(w.clock >= 20, f"hung server: it waited the full 20 s before forcing (clock {w.clock})")
check(rep["forced"] and not rep["polite"] and rep["ok"], "hung server: reported as forced and now stopped")

# an old server (404) is stopped by pid, no waiting
w = World("old")
with w.install():
    rep = fx.stop_everything(R, 8765, proc=None, job=None, protect=ME)
check(w.ev == ["ask_quit", "kill:4242"], f"old server: ask, then kill its reported pid ({w.ev})")
check(rep["old_server"] and rep["forced"] and rep["ok"] and w.clock < 20, "old server: forced at once and stopped")

# an old server that reports no pid: the port's owner is killed
w = World("old")
with w.install(with_pid=False):
    rep = fx.stop_everything(R, 8765, proc=None, job=None, protect=ME)
check(w.ev == ["ask_quit", "kill:4242"] and rep["ok"], f"old server without a pid: the port owner is killed ({w.ev})")

# a foreign program on the port is never killed
w = World("old")
w.owned = False
with w.install(with_pid=False):
    rep = fx.stop_everything(R, 8765, proc=None, job=None, protect=ME)
check(w.ev == ["ask_quit"] and rep["foreign"] == [4242] and not rep["ok"], f"foreign port owner is left alone ({w.ev})")

# nothing running: no ask, no kill
w = World("ok")
w.up = False
with w.install():
    rep = fx.stop_everything(R, 8765, protect=ME)
check(w.ev == [] and rep["ok"] and not rep["forced"], "nothing running: nothing is asked or killed")

# leftovers are swept after the server is gone; one that will not die is reported
w = World("ok", leftovers=[5001, 5002])
with w.install():
    rep = fx.stop_everything(R, 8765, protect=ME)
check(w.ev[0] == "ask_quit" and sorted(rep["swept"]) == [5001, 5002] and rep["ok"], f"leftovers swept after the ask ({w.ev})")
w = World("ok", leftovers=[5003], sticky=True)
with w.install():
    rep = fx.stop_everything(R, 8765, protect=ME)
check(rep["remaining"] == [5003] and not rep["ok"], "a leftover that survives is reported and the stop is not ok")

# ------------------------------------------------------------------ 11-18: kills never raise; failures are recorded


def raises(exc):
    def run(*a, **k):
        raise exc
    return run


with mock.patch.object(fx.subprocess, "run", raises(fx.subprocess.TimeoutExpired("taskkill", 30))):
    check(fx.kill_tree(123456) is False, "kill_tree swallows a taskkill timeout and says False")
with mock.patch.object(fx.subprocess, "run", raises(OSError("no taskkill"))):
    check(fx.kill_tree(123456) is False, "kill_tree swallows an OSError and says False")
with mock.patch.object(fx.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=128)), \
        mock.patch.object(fx, "pid_alive", lambda pid: False):
    check(fx.kill_tree(123456) is True, "taskkill 'not found' for a pid that is gone counts as killed")
with mock.patch.object(fx.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1)), \
        mock.patch.object(fx, "pid_alive", lambda pid: True):
    check(fx.kill_tree(123456) is False, "taskkill failing on a live pid is False")

# an old server whose pid cannot be killed: no exception, the pid is recorded and the stop is not ok
w = World("old")
with w.install():
    with mock.patch.object(fx, "kill_tree", lambda pid: w.ev.append(f"kill:{pid}") and False):
        rep = fx.stop_everything(R, 8765, proc=None, job=None, protect=ME)
check(rep["unkilled"] == [4242] and not rep["ok"] and rep["killed"] == [4242], f"an unkillable pid is recorded {rep}")

# ask_quit sends X-Fixer-Local: 1; a 404 from an older server still counts as "old"
seen_req: list = []


class FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


def fake_urlopen(req, timeout=0):
    seen_req.append(req)
    return FakeResp()


with mock.patch.object(fx.urllib.request, "urlopen", fake_urlopen):
    check(fx.ask_quit(1) == "ok" and seen_req and seen_req[0].get_header("X-fixer-local") == "1"
          and seen_req[0].get_method() == "POST", "ask_quit sends X-Fixer-Local: 1")
with mock.patch.object(fx.urllib.request, "urlopen",
                       raises(fx.urllib.error.HTTPError("http://x", 404, "nf", {}, None))):
    check(fx.ask_quit(1) == "old", "a 404 still means an older server")

# ------------------------------------------------------------------ Tray: Quit stops the server before the icon

order: list[str] = []
fake_rep = {"polite": True, "forced": False, "swept": [], "port_closed": True, "remaining": [], "ok": True, "killed": []}


def fake_stop(*a, **k):
    order.append("stop_everything")
    return dict(fake_rep)


EVENTS.clear()
with mock.patch.object(fx, "stop_everything", fake_stop):
    t = tray.Tray()
    t.icon.stop = lambda: order.append("icon.stop")
    t.quit()
    t.stop_thread.join(10)
check(order == ["stop_everything", "icon.stop"], f"Quit stops the server, then the icon ({order})")
check(t.state == "off" and not t.stopping, "after Quit the tray is grey and not stuck in 'Stopping'")

order.clear()
with mock.patch.object(fx, "stop_everything", fake_stop):
    t.icon.stop = lambda: order.append("icon.stop")
    t.want_quit = t.quitting = False                    # the same tray object is reused after a Quit
    t.turn_off()
    t.stop_thread.join(10)
check(order == ["stop_everything"] and t.state == "off", f"Turn off stops the server and leaves the tray ({order})")

# stop_everything fails with an exception: the icon still goes on Quit, and nothing is left 'Stopping'
order.clear()


def boom(*a, **k):
    order.append("stop_everything")
    raise RuntimeError("x")


with mock.patch.object(fx, "stop_everything", boom):
    t.icon.stop = lambda: order.append("icon.stop")
    t.want_quit = t.quitting = False                    # the same tray object is reused after a Quit
    t.quit()
    t.stop_thread.join(10)
check(order == ["stop_everything", "stop_everything", "icon.stop"] and not t.stopping,
      f"a failing stop is tried once more, then the icon still goes on Quit ({order})")

# 11-18: a stop that reports ok == False is never shown as plain "Off"
order.clear()
results = [False]


def stop_with(*a, **k):
    order.append("stop_everything")
    return dict(fake_rep, ok=results.pop(0) if results else False, port_closed=False)


with mock.patch.object(fx, "stop_everything", stop_with):
    t.icon.stop = lambda: order.append("icon.stop")
    t.want_quit = t.quitting = False                    # the same tray object is reused after a Quit
    t.turn_off()
    t.stop_thread.join(10)
check(order == ["stop_everything"] and t.detail == tray.STOP_FAILED and t.stop_failed,
      f"Turn off falling short shows 'Couldn't stop everything' ({t.detail!r}, {order})")
order.clear()
results[:] = [False, True]
with mock.patch.object(fx, "stop_everything", stop_with):
    t.quit()
    t.stop_thread.join(10)
check(order == ["stop_everything", "stop_everything", "icon.stop"] and t.detail == "Off" and not t.stop_failed,
      f"Quit retries a short stop once; the second one worked: 'Off' ({order})")
order.clear()
results[:] = [False, False]
with mock.patch.object(fx, "stop_everything", stop_with):
    t.want_quit = t.quitting = False                    # the same tray object is reused after a Quit
    t.quit()
    t.stop_thread.join(10)
check(order == ["stop_everything", "stop_everything", "icon.stop"] and t.detail == tray.STOP_FAILED,
      f"Quit: two short stops, the icon goes and the failure is logged ({order})")
check("not everything stopped; trying once more" in tray.LOG.read_text(encoding="utf-8"), "the retry is in tray.log")
t.stop_failed = False

# 11-18: a Quit that falls short is shown before the icon goes: a notification (when pystray has one) and the icon kept
# with its "Couldn't stop everything" title for QUIT_NOTICE_S seconds; a missing or failing notify never stops the quit.
seen_title: list[str] = []


def notify_ok(message, title=None):
    order.append("notify")
    seen_title.append(t.icon.title)
    check("couldn't stop everything" in message.lower() and title == "Fixer Desktop", f"the notice text ({message!r})")


def notify_boom(message, title=None):
    order.append("notify")
    raise RuntimeError("no shell")


for label, notify, want in (("with notify", notify_ok, ["notify"]), ("notify raises", notify_boom, ["notify"]),
                            ("no notify at all", None, [])):
    order.clear()
    SLEPT.clear()
    results[:] = [False, False]
    if notify is None:
        t.icon.__dict__.pop("notify", None)
    else:
        t.icon.notify = notify
    tray._sleep = lambda s: (order.append(f"sleep{s:g}"), SLEPT.append(s))
    with mock.patch.object(fx, "stop_everything", stop_with):
        t.want_quit = t.quitting = False                    # the same tray object is reused after a Quit
        t.quit()
        t.stop_thread.join(10)
    check(order == ["stop_everything", "stop_everything", *want, "sleep5", "icon.stop"],
          f"short Quit ({label}): shown, kept ~5 s, then the icon goes ({order})")
    check(tray.STOP_FAILED in t.icon.title and t.detail == tray.STOP_FAILED,
          f"short Quit ({label}): the icon's title says so while it waits ({t.icon.title!r})")
check(seen_title and tray.STOP_FAILED in seen_title[0], "the title was already set when the notice went up")

# HAS_NOTIFICATION False (a backend without notifications): notify is not called, the wait and the quit still happen
order.clear()
results[:] = [False, False]
t.icon.notify = notify_ok
t.icon.HAS_NOTIFICATION = False
with mock.patch.object(fx, "stop_everything", stop_with):
    t.want_quit = t.quitting = False                    # the same tray object is reused after a Quit
    t.quit()
    t.stop_thread.join(10)
check(order == ["stop_everything", "stop_everything", "sleep5", "icon.stop"], f"no notifications: wait, quit ({order})")
del t.icon.HAS_NOTIFICATION
t.icon.__dict__.pop("notify", None)

# a Quit that worked shows nothing and does not wait
order.clear()
results[:] = [True]
with mock.patch.object(fx, "stop_everything", stop_with):
    t.want_quit = t.quitting = False                    # the same tray object is reused after a Quit
    t.quit()
    t.stop_thread.join(10)
check(order == ["stop_everything", "icon.stop"], f"a clean Quit: no notice, no wait ({order})")
t.stop_failed = False

# 11-18: a second Quit (or Turn off) during the 5-second "couldn't stop" notice does nothing: no second stop, the icon
# goes once; and Turn on clicked there starts nothing
order.clear()
results[:] = [False, False]
late: list = []
prev_sleep = tray._sleep


def sleep_and_click(s):
    order.append(f"sleep{s:g}")
    first = t.stop_thread
    t.quit()
    t.turn_off()
    late.append(t.stop_thread is first and not t.stop_running)


with mock.patch.object(fx, "stop_everything", stop_with), mock.patch.object(t, "reachable", lambda: None), \
        mock.patch.object(tray, "start_in_job") as sij:
    tray._sleep = sleep_and_click
    t.want_quit = t.quitting = False
    t.quit()
    t.stop_thread.join(10)
    t.turn_on()
check(order == ["stop_everything", "stop_everything", "sleep5", "icon.stop"] and late == [True],
      f"a second Quit during the notice: no second stop, one icon.stop ({order}, {late})")
check(not sij.called, "Turn on during / after the Quit's notice starts nothing")

# 11-18: "quitting" is set in the same locked step that clears "stopping": by the time the worker shows "Off" (the
# first moment Turn on could pass the stopping check) quitting is already true
order.clear()
results[:] = [True]
seen_at_off: list = []
real_set_t = t.set


def set_watch(state, detail):
    if state == "off":
        with t.quit_lock:
            seen_at_off.append((t.stopping, t.quitting))
        with mock.patch.object(t, "reachable", lambda: None), mock.patch.object(tray, "start_in_job") as sij2:
            t.turn_on()
            seen_at_off.append(sij2.called)
    real_set_t(state, detail)


t.set = set_watch
with mock.patch.object(fx, "stop_everything", stop_with):
    t.want_quit = t.quitting = False
    t.quit()
    t.stop_thread.join(10)
t.set = real_set_t
check(seen_at_off == [(False, True), False], f"quitting is set together with stopping cleared ({seen_at_off})")
tray._sleep = prev_sleep
t.quitting = False

# 11-18: the race: a Quit clicked while a Turn off's stop is running is never lost
# (a) during the stop: the running worker sees it and removes the icon
order.clear()
t4 = tray.Tray()
t4.icon.stop = lambda: order.append("icon.stop")
gate, inside = threading.Event(), threading.Event()


def slow_stop(*a, **k):
    order.append("stop_everything")
    inside.set()
    gate.wait(10)
    return dict(fake_rep)


with mock.patch.object(fx, "stop_everything", slow_stop):
    t4.turn_off()
    first = t4.stop_thread
    inside.wait(10)
    t4.quit()                                           # the stop is running: this only records the Quit
    check(t4.stop_thread is first, "a Quit during a stop starts no second stop")
    gate.set()
    first.join(10)
check(order == ["stop_everything", "icon.stop"], f"Quit during a Turn off's stop: the icon goes ({order})")

# (b) just after the worker decided (lock released, icon not yet updated): the Quit starts its own stop and quits
order.clear()
t5 = tray.Tray()
t5.icon.stop = lambda: order.append("icon.stop")
real_set = t5.set
fired: list[int] = []


def set_then_quit(state, detail):
    if state == "off" and not fired:
        fired.append(1)
        t5.quit()                                       # lands after the worker's last look at want_quit
    real_set(state, detail)


t5.set = set_then_quit
with mock.patch.object(fx, "stop_everything", fake_stop):
    t5.turn_off()
    first = t5.stop_thread
    first.join(10)
    if t5.stop_thread is not first:
        t5.stop_thread.join(10)
check(order == ["stop_everything", "stop_everything", "icon.stop"] and not t5.stopping and not t5.stop_running,
      f"Quit as a stop finishes: a second stop runs and the icon goes ({order})")

# (c) many Quit / Turn off clicks at once: one stop at a time, and the icon goes exactly once
order.clear()
t6 = tray.Tray()
t6.icon.stop = lambda: order.append("icon.stop")
with mock.patch.object(fx, "stop_everything", fake_stop):
    clicks = [threading.Thread(target=(t6.quit if i % 2 else t6.turn_off)) for i in range(20)]
    for c in clicks:
        c.start()
    for c in clicks:
        c.join(10)
    for _ in range(100):
        th = t6.stop_thread
        th.join(10)
        if th is t6.stop_thread and not t6.stop_running:
            break
check("icon.stop" in order and not t6.stopping, f"a storm of clicks still ends with the icon gone ({order.count('icon.stop')})")

# turn_on is ignored once a Quit is removing the icon
t7 = tray.Tray()
t7.quitting = True
with mock.patch.object(t7, "reachable", lambda: None), mock.patch.object(tray, "start_in_job") as sij:
    t7.turn_on()
check(not sij.called, "Turn on does nothing while the icon is going")
for x in (t4, t5, t6, t7):
    x.job.close()

# turn_on adopts a server that already answers instead of starting a second
t2 = tray.Tray()
with mock.patch.object(t2, "reachable", lambda: {"busy": False}), mock.patch.object(tray.subprocess, "Popen") as pop:
    t2.turn_on()
check(not pop.called, "turn_on does not start a second server when the port already answers")
t.job.close()
t2.job.close()

# ------------------------------------------------------------------ 11-18: turn_on starts the server inside the job
# A throwaway launcher (never the real server) that starts a grandchild at once and writes both pids. Started by
# turn_on (suspended, put in the job, resumed); closing the job must take both. Every pid is killed by us at the end.
import subprocess  # noqa: E402
import time  # noqa: E402

LAUNCHER = Path(TMP) / "launcher_sketch.py"
LAUNCHER.write_text(
    "import os, subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "out = os.path.join(os.path.dirname(os.path.abspath(__file__)), sys.argv[-1] if len(sys.argv) > 2 else 'pids.txt')\n"
    "open(out + '.tmp', 'w').write(f'{os.getpid()} {g.pid}')\n"
    "os.replace(out + '.tmp', out)\n"
    "time.sleep(120)\n", encoding="utf-8")
STARTED: list[int] = []


def read_pids(name: str, secs: float = 30) -> list[int]:
    f = Path(TMP) / name
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if f.exists():
            return [int(x) for x in f.read_text().split()]
        time.sleep(0.1)
    return []


def gone(pid: int, secs: float = 10) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if not fx.pid_alive(pid):
            return True
        time.sleep(0.1)
    return not fx.pid_alive(pid)


def kill_pid(pid: int):
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x0001, False, int(pid))
        if h:
            k.TerminateProcess(ctypes.c_void_p(h), 1)
            k.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        pass


if sys.platform == "win32":
    t3 = tray.Tray()
    real_py, real_server = tray.PY, tray.SERVER
    tray.PY, tray.SERVER = Path(sys.executable), LAUNCHER
    try:
        with mock.patch.object(t3, "reachable", lambda: None):
            t3.turn_on()
        STARTED.append(t3.proc.pid)
        pids = read_pids("pids.txt")
        STARTED.extend(pids)
        check(len(pids) == 2 and all(fx.pid_alive(p) for p in pids),
              f"turn_on: the suspended launcher was resumed and started its grandchild ({pids})")
        t3.job.close()                                  # what happens when the tray ends, however it ends
        check(gone(t3.proc.pid) and all(gone(p) for p in pids), "closing the tray's job takes launcher and grandchild")

        # the fallback: if resuming fails, the suspended one is killed and the old order is used (still in the job)
        job = fx.Job()
        with mock.patch.object(fx, "resume_process", lambda p: False):
            p = tray.start_in_job([sys.executable, "-u", str(LAUNCHER), "x", "pids2.txt"], TMP, job)
        STARTED.append(p.pid)
        pids2 = read_pids("pids2.txt")
        STARTED.extend(pids2)
        check(len(pids2) == 2 and p.poll() is None, f"resume refused: started the old way and running ({pids2})")
        job.close()
        check(gone(p.pid) and all(gone(x) for x in pids2), "the old-way start is still ended with its job")
    finally:
        tray.PY, tray.SERVER = real_py, real_server
        t3.job.close()
        left = [x for x in STARTED if x and fx.pid_alive(x)]
        for x in left:
            kill_pid(x)
        check(not left, f"no process this test started outlived its check ({left})")
check(tray.icon_image("stopping") is not None, "icon_image accepts the 'stopping' state")

print(f"{CHECKS} checks, {len(FAILS)} failed")
if FAILS:
    sys.exit(1)
print("ok")
