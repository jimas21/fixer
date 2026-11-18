"""Starts ComfyUI with a Host check in front of every page it serves (11-18).

Why: ComfyUI's own browser guard only compares Host with Origin. A web page using DNS rebinding (its own hostname
re-pointed at 127.0.0.1) passes that, because both are its hostname; it could then read live previews, the photo being
edited, prompts from /queue and /history, and queue or stop jobs. This refuses any request whose Host is not this
machine's loopback name with ComfyUI's own port: 127.0.0.1, localhost or [::1] (case-insensitive, one trailing dot
allowed). Everything else, a missing Host included, gets a bare 403 with no body.

Fixer starts it in place of main.py, with the same interpreter (ComfyUI's own .venv python, "-s"), the same flags and
cwd = the ComfyUI folder:

    <comfyui>\\.venv\\Scripts\\python.exe -s <root>\\comfy_guard.py --listen 127.0.0.1 --port 8188 ...

It imports none of ComfyUI's modules itself (main.py has set-up that must run before they load). It only reads
--port, wraps aiohttp's web.Application.__init__ so every Application made in this process gets the Host check as its
FIRST middleware (the WebSocket handshake at /ws and sub-applications go through it too), and then runs main.py so it
sees exactly what "python -s main.py ..." would give it: __name__ == "__main__", __file__ and sys.argv[0] = the
absolute main.py path, sys.path[0] = the ComfyUI folder, the flags unchanged. If it cannot do its job it exits with
a non-zero code before ComfyUI starts: never a silent unguarded start.
"""
import os
import runpy
import sys

LOOPBACK = ("127.0.0.1", "localhost", "[::1]")
URL_HOSTS = ("127.0.0.1", "localhost", "::1")      # the same names as yarl gives them back (no brackets)
EXIT_NO_PORT, EXIT_NO_AIOHTTP, EXIT_NOT_PATCHED, EXIT_NO_MAIN = 64, 65, 66, 67


def port_from(argv):
    """The value of --port (also --port=N), as a 1..65535 int; None when it is missing or not a number."""
    val = None
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            val = argv[i + 1]
        elif a.startswith("--port="):
            val = a.split("=", 1)[1]
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 65536 and str(n) == str(val).strip() else None


def host_ok(host, port):
    """True only for 127.0.0.1, localhost or [::1] followed by exactly ':<port>' (case-insensitive; one trailing dot
    allowed on the name). Anything else, a missing or empty Host included, is False."""
    if not host or not isinstance(host, str) or any(c.isspace() for c in host):
        return False
    host = host.lower()
    name, sep, p = host.rpartition(":")
    if not sep or p != str(int(port)):
        return False
    if name.endswith("."):
        name = name[:-1]
    return name in LOOPBACK


def _url_ok(request, port):
    """The request line may be an absolute URL (it then overrides the Host header in aiohttp's request.url): it must
    name the same loopback host and port too."""
    try:
        u = request.url
        h = (u.host or "").lower()
        if h.endswith("."):
            h = h[:-1]
        return h in URL_HOSTS and u.port == port
    except Exception:                                  # noqa: BLE001  (an unparseable target is refused)
        return False


def install(web, port):
    """Wraps web.Application.__init__ so every Application gets the Host check first. Returns the middleware."""

    @web.middleware
    async def comfy_host_guard(request, handler):
        hosts = request.headers.getall("Host", [])
        if len(hosts) != 1 or not host_ok(hosts[0], port) or not _url_ok(request, port):
            return web.Response(status=403)
        return await handler(request)

    original = web.Application.__init__
    if getattr(original, "_fixer_guarded", False):
        return comfy_host_guard

    def __init__(self, *args, middlewares=(), **kwargs):
        original(self, *args, middlewares=[comfy_host_guard, *list(middlewares or ())], **kwargs)

    __init__._fixer_guarded = True
    __init__.__wrapped__ = original
    web.Application.__init__ = __init__
    return comfy_host_guard


def _patched(web, guard):
    """Proves the patch took: a fresh Application has the guard as its first middleware."""
    try:
        app = web.Application()
        return len(app.middlewares) > 0 and app.middlewares[0] is guard
    except Exception:                                  # noqa: BLE001
        return False


def main():
    argv = sys.argv[1:]
    port = port_from(argv)
    if port is None:
        sys.stderr.write("comfy_guard: no --port; not starting ComfyUI\n")
        return EXIT_NO_PORT
    try:
        from aiohttp import web
    except Exception:                                  # noqa: BLE001
        sys.stderr.write("comfy_guard: aiohttp is missing; not starting ComfyUI\n")
        return EXIT_NO_AIOHTTP
    try:
        guard = install(web, port)
    except Exception:                                  # noqa: BLE001
        guard = None
    if guard is None or not _patched(web, guard):
        sys.stderr.write("comfy_guard: could not guard aiohttp; not starting ComfyUI\n")
        return EXIT_NOT_PATCHED
    folder = os.getcwd()                               # Fixer starts this with cwd = the ComfyUI folder
    main_py = os.path.join(folder, "main.py")
    if not os.path.isfile(main_py):
        sys.stderr.write("comfy_guard: no main.py in the working folder; not starting ComfyUI\n")
        return EXIT_NO_MAIN
    # What "python -s main.py ..." would have: the script's folder first on sys.path (not this file's folder), the
    # flags unchanged after argv[0]. run_path sets __name__, __file__ and sys.argv[0] (to main_py) while it runs.
    sys.argv[:] = [main_py, *argv]
    here = os.path.dirname(os.path.abspath(__file__))
    if sys.path and os.path.normcase(os.path.abspath(sys.path[0] or os.curdir)) == os.path.normcase(here):
        sys.path[0] = folder
    else:
        sys.path.insert(0, folder)
    runpy.run_path(main_py, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
