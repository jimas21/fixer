"""Checks of the two local-server locks (11-18), with no GPU, no ComfyUI and no model:

  1. comfy_guard.py, run for real as a child process in front of a tiny stand-in "main.py" (an aiohttp app with one
     GET route, one WebSocket route and a sub-application): only Host 127.0.0.1 / localhost / [::1] with the app's own
     port is answered; the stand-in sees what "python -s main.py ..." would give it; no --port = no start.
  2. llama-server's per-start key, against a stub HTTP server that requires the bearer key on everything but /health:
     Fixer's one client helper, the real post-start proof and the real LlamaBrain._start (Popen faked; the stub plays
     the server), the key hand-off (environment only, never the argument list), /local/state, and the log.

    set FIXER_COMFY=0 && python desktop\\guard_test.py        (the install's venv: aiohttp + cryptography)

FIXER_HOME is a throwaway folder (made and deleted here). Every process this file starts is killed by its own pid.
"""
from __future__ import annotations

import asyncio
import base64
import http.client
import http.server
import json
import logging
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
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="fixer_guard_test_")
os.environ["FIXER_HOME"] = TMP
os.environ["FIXER_COMFY"] = "0"
HERE = Path(__file__).resolve().parent
GUARD = HERE / "comfy_guard.py"
sys.path.insert(0, str(HERE))

FAILS: list[str] = []
CHECKS = 0
STARTED: list[subprocess.Popen] = []


def check(cond, msg: str):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ 1. the ComfyUI host guard

STAND_IN = textwrap.dedent(r'''
    import json, os, sys
    from aiohttp import web
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"name": __name__, "file": __file__, "argv": sys.argv, "path0": sys.path[0],
                   "guard_dir_on_path": any(os.path.normcase(os.path.abspath(p or ".")) == os.path.normcase(sys.argv[-1])
                                            for p in sys.path)}, f)
    port = int(sys.argv[sys.argv.index("--port") + 1])

    async def ok(request):
        return web.Response(text="ok")

    async def ws(request):
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        await sock.send_str("hello")
        await sock.close()
        return sock

    async def sub_ok(request):
        return web.Response(text="sub")

    if __name__ == "__main__":
        app = web.Application()
        app.router.add_get("/ok", ok)
        app.router.add_get("/ws", ws)
        sub = web.Application()
        sub.router.add_get("/x", sub_ok)
        app.add_subapp("/sub", sub)
        web.run_app(app, host="127.0.0.1", port=port, print=None)
''')


def raw(port: int, request: bytes, timeout: float = 5) -> tuple[int, bytes]:
    """Sends request bytes as they are; returns (status, body) of the answer (0 when none)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(request)
        data = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data:
                    head, _, rest = data.partition(b"\r\n\r\n")
                    if head.startswith(b"HTTP/1.1 101"):
                        break
                    length = [int(l.split(b":", 1)[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")]
                    if length and len(rest) >= length[0]:
                        break
        except socket.timeout:
            pass
    head, _, body = data.partition(b"\r\n\r\n")
    try:
        return int(head.split(b" ", 2)[1]), body
    except Exception:
        return 0, body


def get(port: int, path: str, host: str | None) -> tuple[int, bytes]:
    host_line = b"" if host is None else b"Host: " + host.encode() + b"\r\n"
    return raw(port, b"GET " + path.encode() + b" HTTP/1.1\r\n" + host_line + b"Connection: close\r\n\r\n")


def ws_handshake(port: int, host: str) -> int:
    key = base64.b64encode(os.urandom(16))
    req = (b"GET /ws HTTP/1.1\r\nHost: " + host.encode() + b"\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           b"Sec-WebSocket-Key: " + key + b"\r\nSec-WebSocket-Version: 13\r\nOrigin: http://" + host.encode() + b"\r\n\r\n")
    return raw(port, req)[0]


def start_guarded(folder: Path, flags: list[str]) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-s", str(GUARD), *flags], cwd=str(folder), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    STARTED.append(p)
    return p


def kill(p: subprocess.Popen) -> bool:
    if p.poll() is None:
        p.kill()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        return False
    return p.poll() is not None


def case_comfy_guard():
    folder = Path(TMP) / "comfy_stand_in"
    folder.mkdir()
    (folder / "main.py").write_text(STAND_IN, encoding="utf-8")
    port = free_port()
    flags = ["--listen", "127.0.0.1", "--port", str(port), "--disable-auto-launch", "--temp-directory",
             str(folder / "t"), str(HERE)]                    # the last one: the guard's folder, for the path check
    p = start_guarded(folder, flags)
    up = False
    end = time.monotonic() + 30
    while time.monotonic() < end and p.poll() is None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                up = True
                break
        except OSError:
            time.sleep(0.1)
    check(up, f"the guarded stand-in serves on its port (exit {p.poll()})")
    if not up:
        kill(p)
        return
    try:
        for good in (f"127.0.0.1:{port}", f"localhost:{port}", f"LocalHost:{port}", f"[::1]:{port}",
                     f"localhost.:{port}", f"127.0.0.1.:{port}"):
            st, body = get(port, "/ok", good)
            check(st == 200 and body == b"ok", f"Host {good}: 200 ({st})")
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/sub/x")
        r = c.getresponse()
        check(r.status == 200 and r.read() == b"sub", "http.client's own Host (127.0.0.1:port) reaches the sub-app")
        c.close()
        for bad in (f"evil.example:{port}", f"localhost.evil.example:{port}", f"127.0.0.1.evil.example:{port}",
                    "evil.example", f"127.0.0.1:{port + 1 if port < 65535 else 1}", "127.0.0.1:1", "localhost",
                    "127.0.0.1", f"localhost:0{port}", f"0.0.0.0:{port}", f"[::]:{port}", f"localhost..:{port}",
                    f"a@localhost:{port}", f"localhost:{port}:{port}", ""):
            for path in ("/ok", "/sub/x", "/nope"):
                st, body = get(port, path, bad)
                check(st == 403 and body == b"", f"Host {bad!r} on {path}: bare 403 ({st}, {body[:20]!r})")
        check(ws_handshake(port, f"127.0.0.1:{port}") == 101, "WebSocket handshake with the right Host: 101")
        for bad in (f"evil.example:{port}", f"localhost:{port + 1 if port < 65535 else 1}"):
            check(ws_handshake(port, bad) == 403, f"WebSocket handshake with Host {bad}: 403")
        st, _ = raw(port, b"GET /ok HTTP/1.0\r\n\r\n")
        check(st == 403, f"no Host at all (HTTP/1.0): refused ({st})")
        st, _ = raw(port, b"GET /ok HTTP/1.1\r\nConnection: close\r\n\r\n")
        check(st in (400, 403), f"no Host at all (HTTP/1.1): refused ({st})")
        st, _ = raw(port, f"GET /ok HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nHost: evil.example:{port}\r\n"
                          f"Connection: close\r\n\r\n".encode())
        check(st in (400, 403), f"two Host headers: refused ({st})")
        st, _ = raw(port, f"GET http://evil.example:{port}/ok HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          f"Connection: close\r\n\r\n".encode())
        check(st == 403, f"an absolute request line naming another host: 403 ({st})")
        rep = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        check(rep["name"] == "__main__", f"the stand-in runs as __main__ ({rep['name']})")
        check(rep["argv"][0].endswith("main.py") and Path(rep["argv"][0]).resolve() == (folder / "main.py").resolve(),
              f"sys.argv[0] is the stand-in's main.py ({rep['argv'][0]})")
        check(rep["argv"][1:] == flags, "the flags reach main.py unchanged")
        check(Path(rep["file"]).resolve() == (folder / "main.py").resolve(), f"__file__ is main.py ({rep['file']})")
        check(Path(rep["path0"]).resolve() == folder.resolve(), f"sys.path[0] is the stand-in's folder ({rep['path0']})")
        check(not rep["guard_dir_on_path"], "the guard's own folder is not on sys.path (its files cannot shadow ComfyUI's)")
    finally:
        check(kill(p), "the guarded stand-in is killed by its pid")

    # no --port: a non-zero exit before main.py runs
    folder2 = Path(TMP) / "comfy_no_port"
    folder2.mkdir()
    (folder2 / "main.py").write_text(STAND_IN, encoding="utf-8")
    for flags2 in (["--listen", "127.0.0.1"], ["--listen", "127.0.0.1", "--port", "abc"], ["--port"]):
        p2 = start_guarded(folder2, flags2)
        try:
            code = p2.wait(30)
        except subprocess.TimeoutExpired:
            code = None
        kill(p2)
        check(code not in (0, None) and not (folder2 / "report.json").exists(),
              f"{flags2}: exits non-zero ({code}) and never runs main.py")

    # no main.py in the working folder: also non-zero, nothing served
    folder3 = Path(TMP) / "comfy_no_main"
    folder3.mkdir()
    p3 = start_guarded(folder3, ["--port", str(free_port())])
    try:
        code = p3.wait(30)
    except subprocess.TimeoutExpired:
        code = None
    kill(p3)
    check(code not in (0, None), f"no main.py: exits non-zero ({code})")


def case_guard_unit():
    import comfy_guard as g
    check(g.port_from(["--port", "8188"]) == 8188 and g.port_from(["--port=8188"]) == 8188, "--port N / --port=N")
    for bad in ([], ["--port"], ["--port", "x"], ["--port", "0"], ["--port", "70000"], ["--port", "-1"]):
        check(g.port_from(bad) is None, f"no usable --port in {bad}")
    for good in ("127.0.0.1:8188", "LOCALHOST:8188", "[::1]:8188", "localhost.:8188"):
        check(g.host_ok(good, 8188), f"host_ok {good}")
    for bad in ("", None, "localhost", "127.0.0.1", "localhost:8189", "evil:8188", "localhost :8188", "[::1]8188",
                "::1:8188", "localhost.evil:8188"):
        check(not g.host_ok(bad, 8188), f"host_ok refuses {bad!r}")


# ------------------------------------------------------------------ 2. llama-server's key

class Stub(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), StubHandler)
        self.key: str | None = None        # the key it requires (None + ignore=False: refuses all but /health)
        self.ignore = False                # True: a server that answers without any key
        self.seen: list[tuple[str, str | None]] = []


class StubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authorised(self) -> bool:
        auth = self.headers.get("Authorization")
        self.server.seen.append((self.path, auth))
        if self.path == "/health" or self.server.ignore:
            return True
        return self.server.key is not None and auth == f"Bearer {self.server.key}"

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorised():
            return self._send(401, b'{"error":{"code":401,"message":"Invalid API Key"}}')
        if self.path == "/health":
            return self._send(200, b'{"status":"ok"}')
        if self.path == "/props":
            return self._send(200, b'{"n_ctx":4096}')
        self._send(404, b"{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if not self._authorised():
            return self._send(401, b'{"error":{"code":401,"message":"Invalid API Key"}}')
        if self.path == "/v1/chat/completions":
            frames = [{"choices": [{"delta": {"content": "Hello"}}]}, {"choices": [{"delta": {"content": " there"}}]}]
            body = b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames) + b"data: [DONE]\n\n"
            return self._send(200, body, "text/event-stream")
        self._send(404, b"{}")


def start_stub() -> Stub:
    s = Stub()
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


class LogTap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append(str(record.msg))


def case_llama_key():
    import fixer_desktop as fd
    tap = LogTap()
    fd.log.addHandler(tap)
    logging.getLogger().addHandler(tap)
    stub = start_stub()
    port = stub.server_address[1]
    brain = fd.LLAMA
    saved = (fd.subprocess.Popen, fd._JOB, fd.ensure_files, fd._gpu_info, fd._free_port, fd._local_config)
    used_keys: list[str] = []
    try:
        # the one client helper and the proof, straight
        stub.key = "k-" + "x" * 20
        brain.key = stub.key
        check(fd._llama_status(port, "GET", "/props") == 200, "the helper sends the brain's key: 200")
        check(stub.seen[-1] == ("/props", f"Bearer {stub.key}"), "Authorization: Bearer <key> on the wire")
        check(fd._llama_status(port, "GET", "/props", key=None) == 401, "key=None sends none: 401")
        check(fd._llama_status(port, "GET", "/health", key=None) == 200, "/health stays open")
        check(fd._llama_key_proof(port, stub.key) is True, "the proof passes against a server that needs the key")
        brain.key = "wrong"
        check(fd._llama_status(port, "GET", "/props") == 401, "a wrong key is refused")
        brain.key = stub.key
        out: list[dict] = []
        fd._chat_llama_once({"messages": [{"role": "user", "text": "hi"}]}, out.append, threading.Event(), port)
        check("".join(o.get("v", "") for o in out if o.get("t") == "text") == "Hello there"
              and stub.seen[-1] == ("/v1/chat/completions", f"Bearer {stub.key}"),
              f"a chat goes through the helper with the key ({out})")
        stub.ignore = True
        proof = fd._llama_key_proof(port, stub.key)
        check(proof is not True and isinstance(proof, str) and stub.key not in proof,
              f"a server that ignores keys fails the proof ({proof})")
        stub.ignore = False
        brain.key = None

        # the real _start, with Popen faked and the stub playing llama-server on its port
        seen: dict = {}

        class Proc:
            pid = 4242

            def __init__(self):
                self.dead = False

            def poll(self):
                return 0 if self.dead else None

            def terminate(self):
                self.dead = True
                seen["terminated"] = seen.get("terminated", 0) + 1

            kill = terminate

            def wait(self, t=None):
                return 0

        def popen(args, **kw):
            seen["args"], seen["env"] = list(args), kw.get("env")
            env = kw.get("env") or {}
            stub.key = env.get("LLAMA_API_KEY")              # the stub takes its key the way llama-server does
            if stub.key:
                used_keys.append(stub.key)
            return Proc()

        fd.subprocess.Popen = popen
        fd._JOB = types.SimpleNamespace(add=lambda p: None)
        fd.ensure_files = lambda repo, files, status, kind="model": TMP
        fd._gpu_info = lambda: ("test", 10.0, 24.0)
        fd._free_port = lambda: port
        fd._local_config = lambda: {}
        os.environ["LLAMA_ARG_API_KEY_FILE"] = str(Path(TMP) / "not-this-one.txt")   # must not reach the child
        choice = ([], "w.gguf", None)
        brain._start("test/brain-gguf", choice, lambda s: None, gpu=False)
        key1 = brain.key
        check(brain.running() and brain.port == port and brain.key_state() is True, "keyed start: running, proven")
        check(key1 and len(key1) >= 32 and seen["env"].get("LLAMA_API_KEY") == key1
              and "LLAMA_ARG_API_KEY_FILE" not in seen["env"], "the key goes to the child's environment only")
        check(all(key1 not in a for a in seen["args"]) and "--api-key" not in seen["args"]
              and "--api-key-file" not in seen["args"], "the key is never in the argument list")
        i = seen["args"].index("--cors-origins") if "--cors-origins" in seen["args"] else -1
        check(i > 0 and seen["args"][i + 1] == "localhost", "CORS limited to same-origin (--cors-origins localhost)")
        check(os.environ.get("LLAMA_API_KEY") != key1, "our own environment never holds the key")
        check(fd._llama_args("r", Path(TMP), "w.gguf", None, False, 1, keyed=True).count("--cors-origins") == 1,
              "_llama_args adds the CORS limit once")
        brain._start("test/brain-gguf", choice, lambda s: None, gpu=False)
        check(brain.key and brain.key != key1 and brain.key_state() is True, "a fresh key for every start")

        # /local/state: llama_keyed true while running, comfy_guarded null; never the key
        async def state():
            from aiohttp.test_utils import TestClient, TestServer
            client = TestClient(TestServer(fd.app(), host="127.0.0.1"))
            await client.start_server()
            try:
                r = await client.get("/local/state")
                return r.status, await r.text()
            finally:
                await client.close()
        st, text = asyncio.run(state())
        body = json.loads(text) if st == 200 else {}
        check(st == 200 and body.get("llama_keyed") is True and body.get("comfy_guarded", "missing") is None,
              f"/local/state llama_keyed true, comfy_guarded null {body}")
        check(not any(k in text for k in used_keys), "/local/state never carries the key")
        tm = json.dumps(fd._timings_report())
        check(not any(k in tm for k in used_keys), "/local/timings never carries the key")

        # a server that answers without the key: stopped, the usual error, nothing left running
        def popen_ignoring(args, **kw):
            p = popen(args, **kw)
            stub.ignore = True
            return p
        fd.subprocess.Popen = popen_ignoring
        seen.pop("terminated", None)
        try:
            brain._start("test/brain-gguf", choice, lambda s: None, gpu=False)
            err = ""
        except fd.UserFacing as e:
            err = str(e)
        check(err == "The model couldn't start. Try another model." and not brain.running()
              and brain.key is None and brain.key_state() is None and seen.get("terminated", 0) >= 1,
              f"an unkeyed server: stopped, fail closed ({err!r})")
        check(any("key check failed" in line for line in tap.lines), "one warning line says the check failed")
        stub.ignore = False
        fd.subprocess.Popen = popen

        # knob llama_key 0: the old start (no key, no CORS flag, inherited environment), reported as false
        fd._local_config = lambda: {"llama_key": 0}
        brain._start("test/brain-gguf", choice, lambda s: None, gpu=False)
        check(brain.running() and brain.key is None and seen["env"] is None and "--cors-origins" not in seen["args"]
              and brain.key_state() is False, "llama_key 0: old behaviour, llama_keyed false")
        brain.stop()
        check(brain.key_state() is None, "not running: llama_keyed null")
        check(not any(k in line for k in used_keys for line in tap.lines), "the key never appears in the log")
    finally:
        (fd.subprocess.Popen, fd._JOB, fd.ensure_files, fd._gpu_info, fd._free_port, fd._local_config) = saved
        os.environ.pop("LLAMA_ARG_API_KEY_FILE", None)
        brain.proc = None
        brain.stop()
        fd.log.removeHandler(tap)
        logging.getLogger().removeHandler(tap)
        stub.shutdown()
        stub.server_close()


def case_url_host_rule():
    """11-18: _from_this_pc also refuses an absolute request line naming another host."""
    import fixer_desktop as fd

    async def run():
        from aiohttp.test_utils import TestClient, TestServer
        client = TestClient(TestServer(fd.app(), host="127.0.0.1"))
        await client.start_server()
        try:
            port = client.port
            out = {}
            for label, line in (("absolute other host", f"GET http://evil.example:{port}/local/state HTTP/1.1"),
                                ("absolute loopback", f"GET http://127.0.0.1:{port}/local/state HTTP/1.1"),
                                ("plain", "GET /local/state HTTP/1.1")):
                req = f"{line}\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode()
                out[label] = await asyncio.get_running_loop().run_in_executor(None, raw, port, req)
            return out
        finally:
            await client.close()
    out = asyncio.run(run())
    check(out["absolute other host"][0] == 404, f"absolute URL naming another host: 404 ({out['absolute other host'][0]})")
    check(out["absolute loopback"][0] == 200, f"absolute URL naming 127.0.0.1: 200 ({out['absolute loopback'][0]})")
    check(out["plain"][0] == 200, f"plain request line: 200 ({out['plain'][0]})")


def main():
    for name, case in list(globals().items()):
        if name.startswith("case_"):
            case()


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in STARTED:
            if p.poll() is None:
                p.kill()
                try:
                    p.wait(10)
                except Exception:
                    pass
        left = [p.pid for p in STARTED if p.poll() is None]
        check(not left, f"no process this test started outlived it ({left})")
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{CHECKS} checks, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
