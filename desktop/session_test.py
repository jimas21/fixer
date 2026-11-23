"""Checks of the end of a Fixer session (11-18): the phone's "leave", the leave-free rule, /local/state and
/local/quit, the order of the shutdown, the kill-with-me job, and a real boot + quit of the server on a spare port.

    set FIXER_COMFY=0 && python desktop\\session_test.py        (the install's venv: aiohttp + cryptography)

Fakes only for the in-process checks: no GPU, no ComfyUI, no llama-server. FIXER_HOME is a throwaway folder (made and
deleted here). The real-boot check starts the repo's server on a free port with that same throwaway home and
FIXER_COMFY=0; every process this file starts is killed by its own pid before it ends.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="fixer_session_test_")
os.environ["FIXER_HOME"] = TMP
os.environ["FIXER_COMFY"] = "0"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fixer_desktop as fd  # noqa: E402

FAILS: list[str] = []
CHECKS = 0
STARTED: list[int] = []                                # every pid this file started (killed at the end if still up)


def check(cond, msg: str):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


def alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    h = k.OpenProcess(0x00100000 | 0x1000, False, int(pid))     # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        return k.WaitForSingleObject(ctypes.c_void_p(h), 0) != 0   # 0 = signalled = exited
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


def kill_pid(pid: int):
    try:
        if sys.platform == "win32":
            import ctypes
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = ctypes.c_void_p
            h = k.OpenProcess(0x0001, False, int(pid))           # PROCESS_TERMINATE
            if h:
                k.TerminateProcess(ctypes.c_void_p(h), 1)
                k.CloseHandle(ctypes.c_void_p(h))
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def wait_gone(pid: int, secs: float) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


# ------------------------------------------------------------------ the leave-free rule

def case_leave_rule():
    due = fd._leave_free_due
    check(not due(1000, None, 300, False, 0, 0, False), "nobody left: never")
    check(not due(1000, 800, 300, False, 0, 0, False), "inside the grace: not yet")
    check(due(1100, 800, 300, False, 0, 0, False), "grace passed, nothing going on: free")
    check(not due(1100, 800, 0, False, 0, 0, False), "grace 0 turns it off")
    check(not due(1100, 800, 300, True, 0, 0, False), "a job running: wait for it")
    check(not due(1100, 800, 300, False, 2, 0, False), "jobs waiting: wait for them")
    check(not due(1100, 800, 300, False, 0, 1, False), "a chat streaming: wait")
    check(not due(1100, 800, 300, False, 0, 0, True), "the card is locked: wait")


def case_free_after_rules():
    """11-18: the owner's "free my computer after" choice, through the two pure rules (unset / 0 / 1 / 5 / 60)."""
    g, lim, idle = fd._leave_grace_s, fd._idle_limit_s, 1200.0
    check(g(None, 5) == 300 and g(None, 0) == 0, "leave rule unset: leave_free_minutes (5 min; 0 = off)")
    check(g(0, 5) == 0, "leave rule 0: never")
    check(g(1, 5) == 60 and g(5, 5) == 300 and g(60, 5) == 3600, "leave rule 1 / 5 / 60 min: his choice")
    check(lim(None, idle) == idle, "idle rule unset: the 20-min IDLE_UNLOAD")
    check(lim(0, idle) == float("inf"), "idle rule 0: never")
    check(lim(1, idle) == idle and lim(5, idle) == idle, "idle rule 1 / 5 min: never shorter than IDLE_UNLOAD")
    check(lim(60, idle) == 3600, "idle rule 60 min: really an hour")
    p = fd._parse_free_after
    check(p(5) == 5.0 and p(0) == 0.0 and p(1440) == 1440.0 and p(2.5) == 2.5, "real numbers 0..1440 are taken")
    for bad in (True, False, "5", None, float("nan"), float("inf"), -1, 1441, [5], {"m": 5}):
        check(p(bad) is None, f"free_after_min {bad!r} is ignored")


def case_local_host_rule():
    """11-18: the Host test behind every /local page (DNS rebinding): loopback names only, no port or our own."""
    ok, P = fd._local_host_ok, 8765
    for good in ("127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:8765", "LocalHost:8765", "localhost.",
                 "localhost.:8765", "127.0.0.1.", "[::1]", "[::1]:8765"):
        check(ok(good, P), f"Host {good!r} is this PC")
    for bad in ("", None, "evil.example", "evil.example:8765", "localhost.evil.example", "127.0.0.1.evil.example",
                "127.0.0.1.evil.example:8765", "localhost:8766", "127.0.0.1:80", "[::1]:1", "localhost:",
                "localhost:08765", "local host", " localhost", "localhost ", "localhost\t", "a@127.0.0.1",
                "a@localhost:8765", "172.16.0.9:8765", "[::1].:8765", "0.0.0.0", "0.0.0.0:8765", "[::]:8765", "::1", "[::1",
                "localhost..", "127.0.0.2", "[::ffff:127.0.0.1]", "127.1", "2130706433", "localhost:8765:8765"):
        check(not ok(bad, P), f"Host {bad!r} is refused")
    check(ok("localhost:4321", 4321) and not ok("localhost:8765", 4321), "the port is the server's own")


def case_free_after_file():
    """11-18: the saved choice: missing, unreadable or invalid means unset; a good file is read back."""
    f = fd._free_after_file()
    try:
        f.unlink()
    except FileNotFoundError:
        pass
    check(fd._load_free_after() is None, "no file: unset")
    for text in ("", "not json", "[5]", '{"free_after_min": "5"}', '{"free_after_min": -1}', '{"free_after_min": 9999}',
                 '{"free_after_min": true}', '{"other": 5}', "null"):
        f.write_text(text, encoding="utf-8")
        check(fd._load_free_after() is None, f"invalid file {text!r}: unset")
    f.write_bytes(b"\xff\xfe\x00garbage")
    check(fd._load_free_after() is None, "unreadable bytes: unset")
    check(fd._save_free_after(15.0) and fd._load_free_after() == 15.0, "set then reload: 15")
    check(fd._save_free_after(0.0) and fd._load_free_after() == 0.0, "0 (never) is kept as 0, not unset")
    check(fd.FREE_AFTER_MIN is None, "loading never happens at import (the checks' module state is untouched)")
    f.unlink()


async def case_leave_check_choice():
    """11-18: _leave_check uses the owner's choice, and waits while a chat model is starting."""
    fd.QUEUE = fd.JobQueue()
    fd.GPU = asyncio.Lock()
    fd.CHATS_ACTIVE = 0
    freed: list[int] = []
    real_free = fd._leave_free
    fd._leave_free = lambda: freed.append(1)
    try:
        fd.FREE_AFTER_MIN = 1
        fd.LEFT_AT = 100.0
        check(not await fd._leave_check(now=150.0), "choice 1 min: not yet at 50 s")
        fd.LLAMA.starting = True
        check(not await fd._leave_check(now=1000.0) and not freed, "a chat model starting: the pass is skipped")
        fd.LLAMA.starting = False
        check(await fd._leave_check(now=1000.0) and freed == [1], "choice 1 min: freed once the start is over")
        fd.FREE_AFTER_MIN = 0
        fd.LEFT_AT = 100.0
        check(not await fd._leave_check(now=1_000_000.0) and freed == [1], "choice 0: never freed after a leave")
        fd.FREE_AFTER_MIN = 60
        fd.LEFT_AT = 100.0
        check(not await fd._leave_check(now=100.0 + 3599) and await fd._leave_check(now=100.0 + 3600)
              and freed == [1, 1], "choice 60 min: freed at the hour, not before")
    finally:
        fd._leave_free = real_free
        fd.LEFT_AT = None
        fd.FREE_AFTER_MIN = None
        fd.LLAMA.starting = False


def case_video_fallback_says_ten_seconds():
    """11-18: the old video path caps at 10 s and says so in one plain status line when more was asked."""
    from unittest import mock
    seen: list[dict] = []
    ran: list[float] = []
    with mock.patch.dict(sys.modules, {"torch": types.ModuleType("torch")}), \
            mock.patch.object(fd, "_local_complete", lambda repo: True), \
            mock.patch.object(fd, "_video_run", lambda p, emit, cancel, status, mode, repo: ran.append(p["seconds"])), \
            mock.patch.object(fd.ENGINE, "image_repo", None), mock.patch.object(fd.LLAMA, "running", lambda: False):
        fd.job_video({"seconds": 20, "prompt": "x"}, seen.append, threading.Event())
        said = [f.get("v") for f in seen if f.get("t") == "status"]
        check(said == ["This computer can only make a 10-second video this way."] and ran == [20],
              f"20 s asked: one plain line, the fallback otherwise unchanged {said}")
        seen.clear()
        fd.job_video({"seconds": 10, "prompt": "x"}, seen.append, threading.Event())
        fd.job_video({"seconds": "junk", "prompt": "x"}, seen.append, threading.Event())
        check(not seen, f"10 s or an unreadable length: no line {seen}")


async def case_leave_check():
    fd.QUEUE = fd.JobQueue()
    fd.GPU = asyncio.Lock()
    fd.CHATS_ACTIVE = 0
    fd.FREE_AFTER_MIN = None
    freed: list[int] = []
    real_free = fd._leave_free
    fd._leave_free = lambda: freed.append(1)
    try:
        fd.LEFT_AT = 100.0
        check(not await fd._leave_check(now=200.0), "2 min after leaving: nothing freed")
        check(await fd._leave_check(now=401.0) and freed == [1], "5 min after leaving: freed once")
        check(fd.LEFT_AT is None and not await fd._leave_check(now=900.0) and freed == [1], "only once per leave")
        os.environ["FIXER_LEAVE_FREE_MINUTES"] = "0"
        fd.LEFT_AT = 100.0
        check(not await fd._leave_check(now=10_000.0) and not freed[1:], "leave_free_minutes 0: off")
        os.environ["FIXER_LEAVE_FREE_MINUTES"] = "1"
        check(await fd._leave_check(now=161.0) and len(freed) == 2, "the knob is read live (1 min)")
    finally:
        os.environ.pop("FIXER_LEAVE_FREE_MINUTES", None)
        fd._leave_free = real_free
        fd.LEFT_AT = None


def fake_target(op, payload):
    def run(p, emit, cancel):
        for i in range(int(p.get("secs", 0.3) / 0.02)):
            if cancel.is_set():
                return
            emit({"t": "progress", "v": 0.1})
            time.sleep(0.02)
        emit({"t": "image", "v": "img"})
    return run


async def case_leave_waits_for_running_job():
    fd.QUEUE = fd.JobQueue()
    fd.GPU = asyncio.Lock()
    real_target, real_after, real_free = fd._target, fd._after_job, fd._leave_free
    fd._target, fd._after_job = fake_target, lambda kind, nxt: None
    freed: list[int] = []
    fd._leave_free = lambda: freed.append(1)
    agen = fd.queue_runner(None)
    await agen.__anext__()
    try:
        job = fd.Job("image", {"prompt": "x", "secs": 0.6, "slot": "s"}, "p1")
        fd.QUEUE.submit(job, asyncio.get_running_loop())
        await asyncio.sleep(0.15)
        fd.LEFT_AT = time.monotonic() - 3600           # left long ago: only the running job holds it back
        check(fd.QUEUE.busy() and not await fd._leave_check(), "a job running at leave time: not freed under it")
        for _ in range(100):
            if job.state != "running":
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        check(job.state == "ready" and job.results, "the job finished normally and its result is held")
        check(await fd._leave_check() and freed == [1], "the line empty: freed (the held result does not count)")
        check(job.results and fd.QUEUE.find(job.id) is job, "the held result stays held")
    finally:
        try:
            await agen.__anext__()
        except StopAsyncIteration:
            pass
        fd._target, fd._after_job, fd._leave_free = real_target, real_after, real_free
        fd.LEFT_AT = None


def case_leave_free_keeps_brain_off():
    calls: list[str] = []
    real = (fd.ENGINE.unload, fd.COMFY.unload_card, fd.gguf_choice)
    fd.ENGINE.unload = lambda: calls.append("engine")
    fd.COMFY.unload_card = lambda: calls.append("comfy")   # 11-19: leave clears the card; idle stops the engine
    brain = fd.LLAMA
    brain.paused_for, brain.resume_wanted = "video", True
    fd.gguf_choice = lambda repo: calls.append("resume-start") or ([], "w", None)
    try:
        fd._leave_free()
        check(calls[:1] == ["engine"] and calls[-1] == "comfy", f"engine, chat model, ComfyUI freed {calls}")
        check(brain.paused_for is None and not brain.resume_wanted, "a paused chat model is forgotten, not resumed")
        brain.resume()
        check("resume-start" not in calls, "a later resume does not bring it back")
        # a resume already on its way when the leave-free lands: it must give up
        brain.paused_for, brain.resume_wanted = "video", True
        started: list[int] = []
        real_ensure = brain.ensure
        brain.ensure = lambda *a, **k: started.append(1)

        def racing_choice(repo):
            brain.stop_for_good()                      # the leave-free happens between "lift the pause" and "start"
            return ([], "w", None)
        fd.gguf_choice = racing_choice
        brain.resume()
        check(not started, "a resume in flight gives up after a leave-free")
        brain.paused_for, brain.resume_wanted = "video", True
        fd.gguf_choice = lambda repo: ([], "w", None)
        brain.resume()
        check(started == [1], "a normal resume still brings a paused brain back")
        brain.ensure = real_ensure
    finally:
        fd.ENGINE.unload, fd.COMFY.unload_card, fd.gguf_choice = real
        brain.paused_for, brain.resume_wanted = None, False


def case_chat_cancel_and_stream_errors():
    """A cancel ends a chat read that is waiting for the model's first line; an error line or a stream that ends with
    nothing is reported, not an empty reply."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    mode = {"m": "silent"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if mode["m"] == "silent":
                time.sleep(6)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if mode["m"] == "error":
                self.wfile.write(b'data: {"error": {"message": "boom"}}\n\n')
            self.wfile.flush()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port, p = srv.server_address[1], {"messages": [{"role": "user", "text": "hi"}]}
    saved_key, fd.LLAMA.key = fd.LLAMA.key, None
    try:
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.time()
        fd._chat_llama_once(p, lambda o: None, cancel, port)
        check(time.time() - t0 < 4, "a cancel ends a chat that is still waiting for the model's first line")
        for m, what in (("error", "an error line in the stream"), ("empty", "a stream that ends with nothing")):
            mode["m"] = m
            try:
                fd._chat_llama_once(p, lambda o: None, threading.Event(), port)
                err = ""
            except fd.UserFacing as e:
                err = str(e)
            check(err != "", f"{what} is reported to the phone")
    finally:
        fd.LLAMA.key = saved_key
        srv.shutdown()
        srv.server_close()


def case_stop_during_a_start():
    """A chat model start (download, load) does not hold the lock: stop() returns at once and the start gives up."""
    brain = fd.LLAMA
    real = (fd.ensure_files, fd.subprocess.Popen)
    in_download, release = threading.Event(), threading.Event()
    popened: list[int] = []

    def slow_files(repo, files, status, kind="model"):
        in_download.set()
        release.wait(20)                               # a multi-GB download
        return TMP
    fd.ensure_files = slow_files
    fd.subprocess.Popen = lambda *a, **k: popened.append(1)
    result: dict = {}

    def chat():
        try:
            brain.ensure("test/brain-gguf", ([], "w.gguf", None), lambda s: None)
            result["port"] = True
        except fd.UserFacing as e:
            result["err"] = str(e)
    try:
        t = threading.Thread(target=chat)
        t.start()
        check(in_download.wait(10), "the start reached the download")
        check(brain.state() == "loading", "state says loading during the download")
        t0 = time.time()
        brain.stop()
        check(time.time() - t0 < 2, "stop() does not wait for the download")
        release.set()
        t.join(10)
        check("err" in result and not popened and not brain.running(), f"the stopped start gives up and starts nothing ({result})")
        check(brain.state() == "off", "state is off afterwards")
    finally:
        release.set()
        fd.ensure_files, fd.subprocess.Popen = real
        brain.stop()


def case_queued_start_gives_up_after_a_stop():
    """11-22: a chat queued on start_lock behind another start, when the model is then stopped (free memory, leave),
    must not start it again once it gets the lock: it read the generation before it queued."""
    brain = fd.LLAMA
    real = (fd.ensure_files, fd.subprocess.Popen)
    downloads, popened = [], []
    fd.ensure_files = lambda *a, **k: downloads.append(1) or TMP
    fd.subprocess.Popen = lambda *a, **k: popened.append(1)
    result: dict = {}

    def chat():
        try:
            brain.ensure("test/brain-gguf", ([], "w.gguf", None), lambda s: None)
            result["port"] = True
        except fd.UserFacing as e:
            result["err"] = str(e)
    try:
        with brain.start_lock:                         # another start is under way
            t = threading.Thread(target=chat)
            t.start()
            time.sleep(0.4)                            # the chat is now waiting on start_lock
            brain.stop()                               # the model is stopped while it waits
        t.join(10)
        check("stopped before it was ready" in result.get("err", ""), f"the queued chat gives up ({result})")
        check(not downloads and not popened and not brain.running(), "and it starts nothing")
    finally:
        fd.ensure_files, fd.subprocess.Popen = real
        brain.stop()


# ------------------------------------------------------------------ HTTP: leave, /local/state, /local/quit

PHONE = "testphone"
KEY = os.urandom(32)


def write_keys():
    key = base64.urlsafe_b64encode(KEY).decode().rstrip("=")
    fd.KEYS.write_text(json.dumps({"phones": {PHONE: {"name": "test", "key": key}}}), encoding="utf-8")
    fd.KEYSTORE = fd.Keys(fd.KEYS)


async def call(client, op: str, payload: dict, host: str | None = None) -> list[dict]:
    stamp = str(int(time.time()))
    aad = f"{fd.PROTOCOL}|{op}|{PHONE}|{stamp}"
    body = fd.seal(KEY, json.dumps(payload).encode(), aad.encode())
    headers = {"X-Fixer-Phone": PHONE, "X-Fixer-Time": stamp, **({"Host": host} if host else {})}
    r = await client.post(f"/v1/{op}", data=body, headers=headers)
    if r.status != 200:
        return [{"t": "http", "status": r.status}]
    raw = await r.read()
    out, i, seq = [], 0, 0
    while i + 4 <= len(raw):
        n = int.from_bytes(raw[i:i + 4], "big")
        out.append(json.loads(fd.unseal(KEY, raw[i + 4:i + 4 + n], f"{aad}|{seq}".encode())))
        i, seq = i + 4 + n, seq + 1
    return out


async def case_http():
    from aiohttp.test_utils import TestClient, TestServer
    write_keys()
    fd.QUEUE = fd.JobQueue()
    fd.GPU = asyncio.Lock()
    real_status, real_warm = fd.JOBS["status"], fd.job_brain_warm
    fd.JOBS["status"] = lambda p, emit, cancel: emit({"t": "text", "v": "ok"})
    order: list[str] = []
    saved = (fd.QUEUE.stop_all, fd._comfy_stop_now, fd.LLAMA.stop_for_good, fd.WORKER.stop, fd.COMFY.stop, fd._EXIT)
    client = TestClient(TestServer(fd.app(), host="127.0.0.1"))
    await client.start_server()
    try:
        # leave: an info frame then done, loads nothing, starts the grace; any other request cancels it
        fd.LEFT_AT = None
        seen = fd.LAST_SEEN
        frames = await call(client, "leave", {})
        check([f.get("t") for f in frames] == ["info", "done"] and frames[0].get("leaving") is True,
              f"leave answers info(leaving) + done {frames}")
        check(fd.LEFT_AT is not None and fd.LAST_SEEN == seen, "leave starts the grace and is not 'use'")
        check(not fd.QUEUE.jobs, "leave never waits in the line")
        frames = await call(client, "status", {})
        check(frames[-1].get("t") == "done" and fd.LEFT_AT is None, "a later request means someone is back")
        frames = await call(client, "leave", {})
        frames = await call(client, "queue", {"action": "list"})
        check(fd.LEFT_AT is None, "the Queue screen also counts as back")

        # 11-18: "free_after_min" from leave and warm (the app-open warm-up); anything not a real 0..1440 is ignored
        fd.FREE_AFTER_MIN = None
        r = await client.get("/local/state")
        check((await r.json()).get("free_after_min", "missing") is None, "/local/state free_after_min null when unset")
        await call(client, "leave", {"free_after_min": 60})
        check(fd.FREE_AFTER_MIN == 60, "leave carries his choice (60)")
        fd.job_brain_warm = lambda p, emit, cancel: emit({"t": "info", "warm": True})   # never a real model start
        for bad in ("10", True, -5, 5000, None):
            await call(client, "warm", {"free_after_min": bad})
        check(fd.FREE_AFTER_MIN == 60, "warm with a bad value leaves the choice alone")
        await call(client, "warm", {"free_after_min": 0})
        check(fd.FREE_AFTER_MIN == 0, "warm carries his choice (0 = never)")
        await call(client, "status", {"free_after_min": 3})
        check(fd.FREE_AFTER_MIN == 0, "other ops never set it")
        await call(client, "leave", {})
        check(fd.FREE_AFTER_MIN == 0, "a leave without it keeps the last choice")
        await call(client, "status", {})
        r = await client.get("/local/state")
        check((await r.json()).get("free_after_min") == 0, "/local/state reports the choice")
        # 11-18: the choice is saved when it changes and read back by a restart (0 = never is kept, not unset)
        saved_file = fd._free_after_file()
        check(json.loads(saved_file.read_text(encoding="utf-8")) == {"free_after_min": 0.0}
              and fd._load_free_after() == 0.0, "the 0 (never) choice is saved and a restart reads 0, not unset")
        check(not saved_file.with_name(saved_file.name + ".tmp").exists(), "no temp file is left behind")
        await call(client, "leave", {"free_after_min": 60})
        check(fd._load_free_after() == 60.0, "a new choice replaces the saved one")
        await call(client, "warm", {"free_after_min": "bad"})
        check(fd._load_free_after() == 60.0, "an ignored value never touches the saved one")
        # 11-18: a save that failed is tried again the next time the phone sends the value (what was saved is tracked,
        # not just what is in memory); once it is saved, the same value is not written again
        real_save, tries = fd._save_free_after, []
        fails = [True]

        def flaky_save(choice):
            tries.append(choice)
            if fails and fails.pop():
                return False                           # like a failed write: FREE_AFTER_SAVED stays as it was
            return real_save(choice)
        fd._save_free_after = flaky_save
        try:
            await call(client, "leave", {"free_after_min": 20})
            check(fd.FREE_AFTER_MIN == 20 and fd._load_free_after() == 60.0, "a failed save: in memory, not on disk")
            await call(client, "warm", {"free_after_min": 20})
            check(fd._load_free_after() == 20.0 and tries == [20, 20], f"the same value again: saved now {tries}")
            await call(client, "leave", {"free_after_min": 20})
            check(tries == [20, 20], f"saved: not written again {tries}")
        finally:
            fd._save_free_after = real_save
        await call(client, "leave", {"free_after_min": 0})
        await call(client, "status", {})

        # 11-18: DNS rebinding: a hostile name pointed at 127.0.0.1 arrives from loopback with no Origin, but with its
        # own name as Host. Every /local page refuses any Host but loopback; "/" and /v1 do not care (the phone uses
        # the LAN address or a public name).
        for bad in ("evil.example", "evil.example:%d" % client.port, "localhost.evil.example",
                    "127.0.0.1.evil.example", "127.0.0.1:1", "0.0.0.0:%d" % client.port):
            for path in ("/local/state", "/local/timings"):
                r = await client.get(path, headers={"Host": bad})
                check(r.status == 404, f"{path} refused with Host {bad}")
            r = await client.post("/local/unload", headers={"Host": bad})
            check(r.status == 404, f"/local/unload refused with Host {bad}")
        for good in ("localhost:%d" % client.port, "LOCALHOST", "127.0.0.1", "127.0.0.1.:%d" % client.port):
            r = await client.get("/local/state", headers={"Host": good})
            check(r.status == 200, f"/local/state answers with Host {good}")
        r = await client.get("/", headers={"Host": "phone.example"})
        check(r.status == 200, "the plain / page ignores Host")
        frames = await call(client, "status", {}, host="phone.example")
        check(frames and frames[-1].get("t") == "done", f"/v1 works with a non-loopback Host {frames}")
        frames = await call(client, "status", {}, host="203.0.113.5:8765")
        check(frames and frames[-1].get("t") == "done", f"/v1 works with an address as Host {frames}")

        # /local/state: the three new fields; refused through a tunnel and from a web page (Origin)
        r = await client.get("/local/state")
        st = await r.json()
        check(r.status == 200 and st.get("pid") == os.getpid() and st.get("comfy") is False
              and st.get("llama") is False and "busy" in st and "loaded" in st, f"/local/state fields {st}")
        r = await client.get("/local/state", headers={"X-Forwarded-For": "1.2.3.4"})
        check(r.status == 404, "/local/state refused with forwarding headers")
        r = await client.get("/local/state", headers={"Origin": "http://example.test"})
        check(r.status == 404, "/local/state refused from a web page (Origin)")

        # /local/timings: numbers only, newest last, ages not stamps; no field that could carry words
        fd.TIMINGS.clear()
        now = time.monotonic()
        rec = fd._new_timing_record("picture")
        rec.update(model="M1", total_s=12.345, ok=True, at=now - 30, load_s=4.0, draw_s=8.0, loaded=True,
                   prompt="SECRET WORDS", label="SECRET", negative="SECRET")
        fd.TIMINGS.append(rec)
        fd.TIMINGS.append({**fd._new_timing_record("edit"), "model": "M2", "total_s": 3.0, "ok": True, "at": now - 5})
        fd.LEFT_AT = now - 42
        r = await client.get("/local/timings")
        tm = await r.json()
        jobs = tm.get("jobs") or []
        check(r.status == 200 and [j.get("model") for j in jobs] == ["M1", "M2"], f"/local/timings newest last {tm}")
        check(jobs and 29 <= jobs[0].get("age_s", -1) <= 32 and "at" not in jobs[0], "age in seconds, no stamp")
        check(jobs and jobs[0].get("load_s") == 4.0 and jobs[0].get("draw_s") == 8.0 and jobs[0].get("loaded") is True
              and jobs[0].get("enhance") == "off" and "enhance_s" in jobs[0] and jobs[0].get("total_s") == 12.35,
              "load vs draw, enhance and had-to-load are reported")
        check("SECRET" not in json.dumps(tm) and all(set(j) <= set(fd.TIMING_FIELDS) | {"age_s"} for j in jobs),
              "no text field ever leaves (prompt, label, negative stripped)")
        check(40 <= (tm.get("left_s") or 0) <= 45 and tm.get("free_after_min") == 0
              and isinstance(tm.get("idle_s"), int),
              f"/local/timings left_s, free_after_min, idle_s {tm}")
        fd.LEFT_AT = None
        r = await client.get("/local/timings")
        check((await r.json()).get("left_s", "missing") is None, "left_s null when nobody left")
        for h in ({"Origin": "http://example.test"}, {"X-Forwarded-For": "1.2.3.4"}, {"Cf-Ray": "x"}):
            r = await client.get("/local/timings", headers=h)
            check(r.status == 404, f"/local/timings refused with {list(h)[0]}")
        fd.TIMINGS.clear()
        fd.FREE_AFTER_MIN = None

        # /local/quit: refused through a tunnel (nothing runs), accepted from this PC, cleanup in order, then exit
        fd.QUEUE.stop_all = lambda reason="stop_all", drop_held=True: order.append("jobs") or 0
        fd._comfy_stop_now = lambda: order.append("comfy_interrupt")
        fd.LLAMA.stop_for_good = lambda: order.append("llama")
        fd.WORKER.stop = lambda: order.append("worker")
        fd.COMFY.stop = lambda: order.append("comfy")
        fd._EXIT = lambda code: order.append(f"exit{code}")
        LOCAL = {"X-Fixer-Local": "1"}
        for h in ({"Cf-Connecting-Ip": "1.2.3.4"}, {"X-Forwarded-For": "1.2.3.4"}, {"Forwarded": "for=1.2.3.4"},
                  {"Cf-Ray": "x"}, {"Origin": "http://example.test"}, {"Origin": "null"},
                  {"Host": "evil.example"}, {"Host": f"localhost.evil.example:{client.port}"}):
            r = await client.post("/local/quit", headers={**h, **LOCAL})
            check(r.status == 404, f"/local/quit refused with {list(h)[0]}")
        for h in ({}, {"X-Fixer-Local": "0"}, {"X-Fixer-Local": "yes"}):
            r = await client.post("/local/quit", headers=h)
            check(r.status == 404, f"/local/quit refused without X-Fixer-Local: 1 ({h})")
        r = await client.post("/local/unload", headers={"Origin": "http://example.test"})
        check(r.status == 404, "/local/unload refused from a web page (Origin)")
        await asyncio.sleep(0.4)
        check(order == [], "a refused quit does nothing")
        r = await client.post("/local/quit", headers=LOCAL)
        body = await r.json()
        check(r.status == 200 and body == {"quitting": True}, f"/local/quit answers quitting {body}")
        for _ in range(60):
            if any(x.startswith("exit") for x in order):
                break
            await asyncio.sleep(0.05)
        check(order == ["jobs", "comfy_interrupt", "llama", "worker", "comfy", "exit0"],
              f"quit: jobs, ComfyUI's work, chat model, helper, ComfyUI, then exit {order}")
        r = await client.post("/local/quit", headers=LOCAL)
        await asyncio.sleep(0.4)
        check(r.status == 200 and order.count("exit0") == 1, "a second quit does not run it twice")

        # the watchdog: a stop that hangs cannot keep the process alive
        order.clear()
        fd._QUITTING.clear()
        fd.QUIT_GRACE = 0.5
        fd.WORKER.stop = lambda: (order.append("worker-hangs"), time.sleep(2))
        t0 = time.monotonic()
        await client.post("/local/quit", headers=LOCAL)
        for _ in range(60):
            if any(x.startswith("exit") for x in order):
                break
            await asyncio.sleep(0.05)
        exits = [x for x in order if x.startswith("exit")]
        check(exits[:1] == ["exit1"] and time.monotonic() - t0 < 1.5,
              f"a hung stop: the watchdog ends the process ({order})")
        await asyncio.sleep(2.2)                        # let the hung fake return before the fakes are put back
    finally:
        await client.close()
        (fd.QUEUE.stop_all, fd._comfy_stop_now, fd.LLAMA.stop_for_good, fd.WORKER.stop, fd.COMFY.stop,
         fd._EXIT) = saved
        fd.QUEUE.__dict__.pop("stop_all", None)
        fd.JOBS["status"] = real_status
        fd.job_brain_warm = real_warm
        fd.FREE_AFTER_MIN = None
        fd.QUIT_GRACE = 20.0
        fd._QUITTING.clear()
        fd.LEFT_AT = None


def case_shutdown_cleanup_survives_failures():
    calls: list[str] = []
    saved = (fd.LLAMA.stop_for_good, fd.WORKER.stop, fd.COMFY.stop)

    def boom():
        calls.append("llama")
        raise RuntimeError("x")
    fd.LLAMA.stop_for_good = boom
    fd.WORKER.stop = lambda: calls.append("worker")
    fd.COMFY.stop = lambda: calls.append("comfy")
    try:
        fd._shutdown_cleanup()
    finally:
        fd.LLAMA.stop_for_good, fd.WORKER.stop, fd.COMFY.stop = saved
    check(calls == ["llama", "worker", "comfy"], f"one failing stop never skips the next {calls}")


# ------------------------------------------------------------------ real processes

PARENT = textwrap.dedent(r'''
    import multiprocessing as mp, os, subprocess, sys, time
    def nap():
        time.sleep(120)
    if __name__ == "__main__":
        sys.path.insert(0, sys.argv[1])
        import fixer_desktop as fd
        owned = fd._JOB.own()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        helper = mp.get_context("spawn").Process(target=nap, daemon=True)
        helper.start()
        print(f"{int(owned)} {child.pid} {helper.pid}", flush=True)
        time.sleep(120)
''')


def case_children_die_with_parent():
    """A hard-killed server takes every child with it: a plain subprocess and a spawn helper, neither added by hand."""
    if sys.platform != "win32":
        return
    script = Path(TMP) / "parent_sketch.py"
    script.write_text(PARENT, encoding="utf-8")
    env = {**os.environ, "FIXER_HOME": TMP, "FIXER_COMFY": "0"}
    parent = subprocess.Popen([sys.executable, str(script), str(HERE)], stdout=subprocess.PIPE, env=env, text=True)
    STARTED.append(parent.pid)
    line = ""
    t = threading.Thread(target=lambda: globals().__setitem__("_LINE", parent.stdout.readline()), daemon=True)
    t.start()
    t.join(60)
    line = globals().get("_LINE", "")
    parts = line.split()
    if len(parts) != 3:
        check(False, f"the parent sketch did not report its children ({line!r})")
        return
    owned, child, helper = int(parts[0]), int(parts[1]), int(parts[2])
    STARTED.extend([child, helper])
    check(owned == 1, "the server can put itself in its kill-with-me job (nested under whatever holds it)")
    check(alive(child) and alive(helper), "both children are running")
    parent.kill()                                      # hard kill: no finally, no cleanup code runs
    parent.wait(10)
    check(wait_gone(child, 10), "a plain child died with the hard-killed parent")
    check(wait_gone(helper, 10), "a spawn helper died with the hard-killed parent")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def case_real_boot_and_quit():
    """The repo's server, booted for real on a spare port (FIXER_COMFY=0, throwaway home): /local/state, then
    /local/quit; the process must be gone and the port closed within 15 s."""
    port = free_port()
    if port == 8765:
        port = free_port()
    home = Path(TMP) / "boot_home"
    home.mkdir(exist_ok=True)
    (home / "free_after.json").write_text('{"free_after_min": 0}', encoding="utf-8")   # a choice saved before a restart
    env = {**os.environ, "FIXER_HOME": str(home), "FIXER_COMFY": "0", "FIXER_PORT": str(port),
           "FIXER_HOST": "127.0.0.1"}
    proc = subprocess.Popen([sys.executable, "-u", str(HERE / "fixer_desktop.py")], env=env, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    STARTED.append(proc.pid)
    st = None
    end = time.monotonic() + 60
    while time.monotonic() < end and proc.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/local/state", timeout=2) as r:
                st = json.loads(r.read())
                break
        except Exception:
            time.sleep(0.3)
    check(st is not None, "the server booted and answered /local/state")
    if st is None:
        return
    pid = int(st.get("pid") or 0)
    STARTED.append(pid)
    check(pid > 0 and st.get("comfy") is False and st.get("llama") is False, f"real /local/state {st}")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/local/timings", timeout=5) as r:
        tm = json.loads(r.read())
    check(tm.get("jobs") == [] and "left_s" in tm and "idle_s" in tm, f"real /local/timings {tm}")
    check(st.get("free_after_min") == 0, f"the saved 'never' choice is back after a restart ({st.get('free_after_min')!r})")

    # 11-18: DNS rebinding: the same loopback connection with a forged Host is refused by every /local page
    def status(path: str, host: str, post: bool = False) -> int:
        h = {"Host": host, **({"X-Fixer-Local": "1"} if post else {})}
        rq = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"" if post else None, headers=h,
                                    method="POST" if post else "GET")
        try:
            with urllib.request.urlopen(rq, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
    for forged in (f"evil.example:{port}", f"localhost.evil.example:{port}"):
        check(status("/local/state", forged) == 404, f"real /local/state refused with Host {forged}")
        check(status("/local/timings", forged) == 404, f"real /local/timings refused with Host {forged}")
        check(status("/local/quit", forged, post=True) == 404, f"real /local/quit refused with Host {forged}")
    check(status("/local/state", f"localhost:{port}") == 200, "real /local/state answers with Host localhost")
    check(port_open(port) and proc.poll() is None, "a refused forged quit left the server running")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/local/quit", data=b"", method="POST")
    try:
        urllib.request.urlopen(req, timeout=5).read()
        refused = False
    except urllib.error.HTTPError as e:
        refused = e.code == 404
    check(refused, "real quit without X-Fixer-Local is refused (404)")
    import fixerctl
    t0 = time.monotonic()
    answer = fixerctl.ask_quit(port)                   # sends X-Fixer-Local: 1
    check(answer == "ok", f"fixerctl.ask_quit stops the real server ({answer})")
    gone = wait_gone(pid, 15) and wait_gone(proc.pid, 5)
    took = time.monotonic() - t0
    check(gone, "the server process is gone after quit")
    check(not port_open(port), "the port is closed after quit")
    print(f"real boot: quit took {took:.1f} s, pid {pid}, port {port}")


def cleanup_started():
    left = [p for p in STARTED if p and alive(p)]
    for p in left:
        kill_pid(p)
    if left:
        print("killed leftover test processes:", left)
    check(not left, f"no process this test started outlived its check ({left})")


async def main():
    for name, case in list(globals().items()):
        if name.startswith("case_"):
            if asyncio.iscoroutinefunction(case):
                await asyncio.wait_for(case(), timeout=60)
            else:
                await asyncio.get_running_loop().run_in_executor(None, case)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        cleanup_started()
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{CHECKS} checks, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
