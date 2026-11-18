"""fixerctl: the ONE way to control the Fixer Desktop install, for a person or a coding session (11-18).

    python fixerctl.py status            who is running: trays, server, port, leftovers (read-only)
    python fixerctl.py stop              stop everything (server, ComfyUI, chat model) and end every tray
    python fixerctl.py start             start exactly one tray, detached, only if none is running
    python fixerctl.py restart           stop, then start
    python fixerctl.py deploy [--force]  copy this folder's files into the install, check them, restart
                                         (refuses while a job is running or waiting unless --force)

Never kill Fixer processes by hand: the tray owns the server, and hand kills are how duplicates got made.
The install folder comes from FIXER_HOME (default C:\\FixerDesktop), the port from FIXER_PORT (default 8765).
Standard library only, so it runs from the repo's desktop folder and from the install alike. tray.py imports this
file for the shared stop routine, the single-instance mutex and the job object.

Note on C:\\FixerDesktop\\venv: it is a uv venv, so venv\\Scripts\\python.exe is a small launcher that starts the real
Python 3.12 as a CHILD. One logical process therefore shows up as two in a process list, and Popen.pid is the launcher's.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("FIXER_HOME", r"C:\FixerDesktop"))
PORT = int(os.environ.get("FIXER_PORT", "8765"))
TRAY_MUTEX = os.environ.get("FIXER_TRAY_MUTEX", r"Global\FixerDesktopTray")
NO_WINDOW = 0x08000000
DEPLOY_FILES = ["fixer_desktop.py", "tray.py", "taehv.py", "pair.py", "fixerctl.py", "client_test.py", "trace_test.py"]
SHELLS = {"powershell.exe", "pwsh.exe", "cmd.exe", "conhost.exe", "bash.exe", "sh.exe"}
OWN_SCRIPTS = re.compile(r"""(^|[\\/"\s])(tray|pair|fixerctl)\.py\b""")

_sleep = time.sleep          # the tests swap these two for a fake clock
_mono = time.monotonic


# ------------------------------------------------------------------ the matching rule (pure)

def _n(p) -> str:
    return str(p or "").lower().replace("/", "\\").strip().strip('"')


def _under(path: str, root: str) -> bool:
    """True when path is root itself or inside it. C:\\FixerDesktop2 is NOT under C:\\FixerDesktop."""
    return path == root or path.startswith(root.rstrip("\\") + "\\")


def is_leftover(pid, exe, cmdline, ppid, root, protect=()) -> bool:
    """Does this process belong to the Fixer Desktop install at `root` and have nothing to do with the tray
    tooling? Includes: llama-server and ComfyUI's python under root, the venv python running fixer_desktop.py, and
    the base-python child of any launcher whose command line names root's fixer_desktop.py, comfyui or comfy-private.
    Excludes: pids in `protect` (this process and its launcher parent), anything running tray.py / pair.py /
    fixerctl.py, shells, and anything not under root. `ppid` is part of the signature so callers pass the whole row;
    protection of a launcher parent is done by putting its pid in `protect`."""
    if pid in protect:
        return False
    r = _n(root)
    e = _n(exe)
    c = _n(cmdline)
    name = e.rsplit("\\", 1)[-1]
    if name in SHELLS:
        return False
    if OWN_SCRIPTS.search(c):
        return False
    named = re.compile(re.escape(r) + r"\\(fixer_desktop\.py|comfyui|comfy-private)(?=$|[\\\"'\s])")
    if e and _under(e, r):
        if name in ("python.exe", "pythonw.exe") and _under(e, r + "\\venv\\scripts"):
            return bool(named.search(c)) and "fixer_desktop.py" in c      # the venv launcher of the server
        return _under(e, r + "\\llama") or _under(e, r + "\\comfyui") or _under(e, r + "\\comfy-private") \
            or bool(named.search(c))
    # exe outside root (the base-python child of a launcher): only a python may match by command line, so an editor or
    # viewer that merely has root's files in its arguments is never swept
    return name in ("python.exe", "pythonw.exe") and bool(named.search(c))


def is_tray(exe, cmdline, root=None) -> bool:
    """A python process running THIS install's tray.py (under root) or this repo's desktop\\tray.py; the launcher and
    its base-python child both match. Another project's tray.py does not."""
    e = _n(exe).rsplit("\\", 1)[-1]
    if e not in ("python.exe", "pythonw.exe"):
        return False
    r = _n(ROOT if root is None else root)
    mine = _n(HERE / "tray.py")
    for m in re.finditer(r'"([^"]*tray\.py)"|(\S*tray\.py)\b', str(cmdline or ""), re.I):
        p = _n(m.group(1) or m.group(2))
        if p == mine or _under(p, r):
            return True
    return False


# ------------------------------------------------------------------ windows plumbing

def _k32():
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    V = ctypes.c_void_p
    k.CreateMutexW.restype, k.CreateMutexW.argtypes = V, [V, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenMutexW.restype, k.OpenMutexW.argtypes = V, [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.CloseHandle.argtypes = [V]
    k.OpenProcess.restype, k.OpenProcess.argtypes = V, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.GetExitCodeProcess.argtypes = [V, ctypes.POINTER(wintypes.DWORD)]
    k.CreateJobObjectW.restype, k.CreateJobObjectW.argtypes = V, [V, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [V, ctypes.c_int, V, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [V, V]
    k.TerminateJobObject.argtypes = [V, ctypes.c_uint]
    return k


def acquire_single(name: str = TRAY_MUTEX):
    """Takes the named mutex. Returns a handle (keep it alive for the life of the process), or None when another
    process already holds it. If the mutex cannot be made for another reason, carries on (returns 1)."""
    k = _k32()
    h = k.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not h:
        return None if err == 5 else 1                  # 5 = access denied: someone else's, so it exists
    if err == 183:                                      # ERROR_ALREADY_EXISTS
        k.CloseHandle(h)
        return None
    return h


def release_single(h):
    if h and h != 1:
        _k32().CloseHandle(h)


def single_held(name: str = TRAY_MUTEX) -> bool:
    """Is some process holding the tray mutex right now? (Does not take it.)"""
    k = _k32()
    h = k.OpenMutexW(0x00100000, False, name)           # SYNCHRONIZE
    if h:
        k.CloseHandle(h)
        return True
    return ctypes.get_last_error() == 5


class Job:
    """A Windows job object that kills everything in it when its handle closes (so when the owner dies, however).
    Nested jobs are fine on Windows 10+; the server keeps its own job for its children."""

    def __init__(self):
        self.h = None
        if sys.platform != "win32":
            return
        try:
            from ctypes import wintypes
            k = self._k = _k32()

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            h = k.CreateJobObjectW(None, None)
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if h and k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.h = h
        except Exception:
            self.h = None

    def add(self, proc) -> bool:
        """Puts a Popen in the job. False (and carries on) if Windows refuses."""
        if not self.h:
            return False
        try:
            return bool(self._k.AssignProcessToJobObject(self.h, int(proc._handle)))
        except Exception:
            return False

    def terminate(self):
        if self.h:
            try:
                self._k.TerminateJobObject(self.h, 1)
            except Exception:
                pass

    def close(self):
        """Closing the handle kills whatever is still inside."""
        if self.h:
            try:
                self._k.CloseHandle(self.h)
            except Exception:
                pass
            self.h = None


def pid_alive(pid) -> bool:
    if not pid:
        return False
    k = _k32()
    h = k.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5             # access denied means it exists
    try:
        from ctypes import wintypes
        code = wintypes.DWORD()
        return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
    finally:
        k.CloseHandle(h)


def kill_tree(pid) -> bool:
    """taskkill /T /F. Never raises (11-18): False when taskkill hung or could not run, or failed and the pid lives on."""
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=NO_WINDOW, capture_output=True,
                           timeout=30)
        return r.returncode == 0 or not pid_alive(pid)   # "not found" means it is already gone
    except (subprocess.TimeoutExpired, OSError, Exception):  # noqa: BLE001 - a stop must never die here
        return False


def resume_process(proc) -> bool:
    """11-18: resumes a Popen started with CREATE_SUSPENDED. Popen closes the primary thread's handle, so this uses
    ntdll's NtResumeProcess on the process handle (present in every Windows since XP). False if it could not."""
    if sys.platform != "win32":
        return False
    try:
        nt = ctypes.WinDLL("ntdll")
        nt.NtResumeProcess.restype, nt.NtResumeProcess.argtypes = ctypes.c_long, [ctypes.c_void_p]
        return nt.NtResumeProcess(ctypes.c_void_p(int(proc._handle))) == 0
    except Exception:                                   # noqa: BLE001
        return False


def _ps(script: str, env: dict | None = None, timeout: int = 30) -> str:
    e = dict(os.environ, **(env or {}))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script],
                       capture_output=True, creationflags=NO_WINDOW, timeout=timeout, env=e)
    return r.stdout.decode("utf-8", "replace")


# ------------------------------------------------------------------ the server

def port_open(port: int = PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def state(port: int = PORT) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/local/state", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def ask_quit(port: int = PORT) -> str:
    """Asks the server to shut itself down. 'ok' (it said so), 'old' (no /local/quit: an older server) or 'down'."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/local/quit", data=b"", method="POST",
                                     headers={"X-Fixer-Local": "1"})     # 11-18: the server refuses it without
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return "ok"
    except urllib.error.HTTPError:
        return "old"
    except Exception:
        return "down"


def port_pids(port: int = PORT) -> list[int]:
    try:
        out = _ps(f"(Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue)"
                  ".OwningProcess", timeout=20)
    except Exception:
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def process_rows(root=ROOT) -> list[dict]:
    """One Get-CimInstance call, filtered inside PowerShell: processes that mention root or a tray/pair/fixerctl
    script. Returns [{pid, ppid, name, exe, cmd}]. Empty list if the call fails."""
    script = (
        "$r=$env:FX_ROOT;"
        "$rows=@(Get-CimInstance Win32_Process | Where-Object {"
        "($_.ExecutablePath -and $_.ExecutablePath.IndexOf($r,[StringComparison]::OrdinalIgnoreCase) -ge 0) -or "
        "($_.CommandLine -and ($_.CommandLine.IndexOf($r,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or "
        "$_.CommandLine -match 'tray\\.py|fixerctl\\.py|pair\\.py'))} | "
        "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine);"
        "ConvertTo-Json -InputObject $rows -Compress")
    try:
        data = json.loads(_ps(script, {"FX_ROOT": str(root)}, timeout=40) or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return [{"pid": d.get("ProcessId"), "ppid": d.get("ParentProcessId"), "name": d.get("Name") or "",
             "exe": d.get("ExecutablePath") or "", "cmd": d.get("CommandLine") or ""} for d in data]


def find_leftovers(root=ROOT, protect=(), rows=None) -> list[dict]:
    rows = process_rows(root) if rows is None else rows
    return [d for d in rows if is_leftover(d["pid"], d["exe"], d["cmd"], d["ppid"], root, protect)]


def find_trays(protect=(), rows=None) -> list[dict]:
    rows = process_rows(ROOT) if rows is None else rows
    return [d for d in rows if d["pid"] not in protect and is_tray(d["exe"], d["cmd"])]


def install_owned(pid, root=ROOT, protect=()) -> bool:
    """Is this pid one of the install's own processes (is_leftover rule)? Used before killing a port owner."""
    for d in process_rows(root):
        if d["pid"] == pid:
            return is_leftover(d["pid"], d["exe"], d["cmd"], d["ppid"], root, protect)
    return False


# ------------------------------------------------------------------ stop everything

def _gone(port, proc, spid) -> bool:
    return (not port_open(port)) and (proc is None or proc.poll() is not None) and not (spid and pid_alive(spid))


def _wait(port, proc, spid, secs) -> bool:
    end = _mono() + secs
    while True:
        if _gone(port, proc, spid):
            return True
        if _mono() >= end:
            return False
        _sleep(0.5)


def stop_everything(root=ROOT, port=PORT, proc=None, job=None, protect=(), wait=20.0, say=lambda s: None) -> dict:
    """The one stop routine (tray Turn off, tray Quit, fixerctl stop).
    1. ask the server to quit itself (/local/quit) and wait up to `wait` s for the port and the tray's own child to go;
    2. if that failed (or the server is an older one with no /local/quit): terminate the tray's job, taskkill the
       child, else the pid /local/state reports, else whatever owns the port;
    3. sweep leftovers that belong to this install (is_leftover) and kill them;
    4. confirm. Returns what it found. report["ok"] is True when the port is closed and nothing is left.
    11-18: never raises on a kill: a pid that could not be killed is listed in report["unkilled"]."""
    protect = set(protect)
    rep = {"polite": False, "old_server": False, "forced": False, "killed": [], "swept": [], "port_closed": False,
           "remaining": [], "ok": False, "foreign": [], "unkilled": []}

    def kill(pid, into):
        """11-18: kill_tree, noting the pid in rep["unkilled"] when it could not be killed (ok is then False)."""
        if not kill_tree(pid):
            rep["unkilled"].append(pid)
            say(f"could not kill pid {pid}")
        rep[into].append(pid)

    st = state(port)
    spid = (st or {}).get("pid")
    alive = port_open(port) or (proc is not None and proc.poll() is None) or bool(spid and pid_alive(spid))
    clean = not alive
    if alive:
        answer = ask_quit(port)
        if answer == "ok":
            say("asked the server to quit")
            clean = _wait(port, proc, spid, wait)
            rep["polite"] = clean
        else:
            rep["old_server"] = answer == "old"
            say("older server, no quit request" if answer == "old" else "server not answering")
            clean = _gone(port, proc, spid)
    if not clean:
        rep["forced"] = True
        say("forcing it to stop")
        if job is not None:
            job.terminate()
        if proc is not None and proc.poll() is None:
            kill(proc.pid, "killed")
        if spid and pid_alive(spid) and spid not in protect:
            kill(spid, "killed")
        elif not spid and port_open(port):
            for pid in port_pids(port):
                if pid in protect:
                    continue
                if install_owned(pid, root, protect):
                    kill(pid, "killed")
                else:
                    rep["foreign"].append(pid)
                    say(f"port {port} is held by another program (pid {pid}); not touching it")
        _wait(port, proc, spid, 5)
    left = find_leftovers(root, protect)
    for d in left:
        kill(d["pid"], "swept")
    if left:
        say("swept leftovers: " + ", ".join(f'{d["name"]} {d["pid"]}' for d in left))
        _sleep(1)
    rep["remaining"] = [d["pid"] for d in find_leftovers(root, protect)]
    rep["port_closed"] = not port_open(port)
    # 11-18: a pid we could not kill that is still alive is not a stop (one that died anyway does not count)
    still = [p for p in rep["unkilled"] if pid_alive(p)]
    rep["ok"] = rep["port_closed"] and not rep["remaining"] and not still
    return rep


# ------------------------------------------------------------------ starting a tray detached

def launch_detached(exe, args, cwd, say=lambda s: None) -> tuple[str, int | None]:
    """Starts a process that does NOT die when the caller's job object closes. Returns (method, pid).
    1. Win32_Process.Create through CIM: the new process is a child of the WMI host, outside the caller's job.
    2. Popen with DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB (refused when the caller's
       job forbids breakaway); last resort the same without breakaway."""
    cmd = subprocess.list2cmdline([str(exe)] + [str(a) for a in args])
    try:
        out = _ps("$ErrorActionPreference='Stop';$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                  "-Arguments @{CommandLine=$env:FX_CMD;CurrentDirectory=$env:FX_CWD};"
                  "\"$($r.ReturnValue) $($r.ProcessId)\"", {"FX_CMD": cmd, "FX_CWD": str(cwd)}, timeout=40).split()
        if len(out) == 2 and out[0] == "0" and out[1].isdigit() and int(out[1]) > 0:
            return "cim", int(out[1])
        say(f"CIM create gave {out}")
    except Exception as ex:
        say(f"CIM create failed: {type(ex).__name__}")
    base = 0x00000008 | 0x00000200                       # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for flags, label in ((base | 0x01000000, "popen-breakaway"), (base, "popen")):
        try:
            p = subprocess.Popen([str(exe)] + [str(a) for a in args], cwd=str(cwd), creationflags=flags,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True)
            return label, p.pid
        except OSError as ex:
            say(f"{label} refused: {ex}")
    return "none", None


# ------------------------------------------------------------------ commands

def _me() -> set:
    return {os.getpid(), os.getppid()}


def cmd_status(args) -> int:
    rows = process_rows(ROOT)
    me = _me()
    trays = find_trays(me, rows)
    print(f"install: {ROOT}   port: {PORT}")
    print("trays: " + (", ".join(str(t["pid"]) for t in trays) if trays else "none")
          + ("   (mutex held)" if single_held() else "   (mutex free)"))
    st = state(PORT)
    print(f"port {PORT}: " + ("answering" if port_open(PORT) else "closed"))
    if st is not None:
        print(f"server pid: {st.get('pid', 'not reported (older server)')}")
        print("state: " + json.dumps(st, sort_keys=True))
    else:
        print("server: no answer on /local/state")
    left = find_leftovers(ROOT, me, rows)
    print("install processes (server, chat model, ComfyUI): " + ("none" if not left else ""))
    for d in left:
        print(f"  pid {d['pid']}  {d['name']}  {d['cmd'][:110]}")
    return 0


def cmd_stop(args) -> int:
    rep = stop_everything(ROOT, PORT, protect=_me(), say=print)
    trays = find_trays(_me())
    for t in trays:
        kill_tree(t["pid"])
    if trays:
        print("ended trays: " + ", ".join(str(t["pid"]) for t in trays))
        _sleep(1)
        rep2 = find_leftovers(ROOT, _me())              # a tray's end closes its job: catch anything still there
        for d in rep2:
            kill_tree(d["pid"])
    left_trays = find_trays(_me())
    print(f"server: {'stopped by asking it' if rep['polite'] else 'stopped by force' if rep['forced'] else 'was not running'}")
    print(f"port {PORT}: {'closed' if rep['port_closed'] else 'STILL OPEN'}")
    if rep["swept"]:
        print("swept leftovers: " + ", ".join(map(str, rep["swept"])))
    if rep["remaining"] or left_trays:
        print("STILL RUNNING: " + ", ".join(map(str, rep["remaining"] + [t["pid"] for t in left_trays])))
    ok = rep["ok"] and not left_trays
    print("everything is stopped" if ok else "NOT everything stopped")
    return 0 if ok else 1


def cmd_start(args) -> int:
    if single_held() or find_trays(_me()):
        print("a tray is already running; not starting another")
        return 0
    tray = ROOT / "tray.py"
    if not tray.is_file():
        print(f"no tray.py in {ROOT}")
        return 1
    exe = ROOT / "venv" / "Scripts" / "pythonw.exe"
    if not exe.is_file():
        exe = ROOT / "venv" / "Scripts" / "python.exe"
    if not exe.is_file():
        print(f"no venv python in {ROOT}")
        return 1
    method, pid = launch_detached(exe, [tray], ROOT, say=print)
    if pid is None:
        print("could not start the tray")
        return 1
    end = _mono() + 25
    while _mono() < end:
        if single_held():
            print(f"tray started (pid {pid}, via {method})")
            return 0
        _sleep(0.5)
    print(f"tray process {pid} started ({method}) but did not take its mutex within 25 s")
    return 1


def cmd_restart(args) -> int:
    rc = cmd_stop(args)
    if rc:
        return rc
    return cmd_start(args)


def _differs(a: Path, b: Path) -> bool:
    return not b.is_file() or a.read_bytes() != b.read_bytes()


def cmd_deploy(args) -> int:
    if HERE.resolve() == ROOT.resolve():
        print("this fixerctl.py already runs from the install; run it from the repo's desktop folder to deploy")
        return 1
    st = state(PORT)
    if st and (st.get("busy") or st.get("waiting", 0) > 0) and "--force" not in args:
        print(f"refusing: a job is running or waiting (busy={st.get('busy')}, waiting={st.get('waiting')}). "
              "Wait for it, or use --force.")
        return 2
    py = ROOT / "venv" / "Scripts" / "python.exe"
    if not py.is_file():
        print(f"no venv python in {ROOT}")
        return 1
    todo = [n for n in DEPLOY_FILES if (HERE / n).is_file() and _differs(HERE / n, ROOT / n)]
    for n in todo:                                       # check the new files BEFORE they replace working ones
        r = subprocess.run([str(py), "-c", "import sys;compile(open(sys.argv[1],'rb').read(),sys.argv[1],'exec')", str(HERE / n)], capture_output=True, text=True, timeout=60,
                           creationflags=NO_WINDOW)
        if r.returncode:
            print(f"{n} does not compile; nothing was copied:\n{r.stderr.strip()[-400:]}")
            return 1
    for n in todo:
        shutil.copyfile(HERE / n, ROOT / n)
        print(f"copied {n}")
    if not todo:
        print("install already matches this folder")
    return cmd_restart(args)


COMMANDS = {"status": cmd_status, "stop": cmd_stop, "start": cmd_start, "restart": cmd_restart, "deploy": cmd_deploy}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(__doc__.split("\n\n")[0])
        print("commands: " + ", ".join(COMMANDS))
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
