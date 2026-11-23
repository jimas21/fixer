"""At-rest encryption for the server's small config files — keys.json (the pairing keys), macros.json (the owner's
macros) and library.json (the LoRA catalog).

These are persistent config, so they can't live only in RAM like request content does; they must sit on disk. This
module seals them with Windows DPAPI (CryptProtectData, **user scope**) through ctypes — no third-party dependency,
and, crucially, **no app-managed key file**: the decryption capability is held by the OS and bound to the Windows
account the server runs as. So there is no "secret zero" plaintext key sitting next to the data (which is exactly the
trap that made encrypting one file while keys.json stayed plaintext pointless).

Design notes:
  * User scope (not machine): an offline-disk attacker needs the Windows account's secret (its password) to decrypt;
    another local user can't read it. The server and pair.py both run as that user, and DPAPI user-scope blobs
    decrypt across processes of the same user (verified).
  * A sealed file begins with a short MAGIC marker so a legacy *plaintext* file is told apart from a sealed blob and
    read transparently — the migration is seamless and reads never break.
  * write_text verifies it can decrypt what it just sealed BEFORE replacing the file, so a config is never left in a
    state this machine can't read back.
  * If DPAPI ever can't decrypt (e.g. a full Windows password *reset*, not a change — rare), keys.json becomes
    unreadable and the only consequence is re-pairing the phone from the QR. No silent data loss. Writers refuse
    to touch an unreadable file; the way out is set_aside() (`python pair.py --reset keys` / `--reset library`),
    which renames the file and keeps it.

This module stays dependency-free and import-light so pair.py (tkinter/qrcode only) can use it without pulling in the
heavy server module.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import threading
import time
from pathlib import Path

try:                                     # Windows: the cross-process lock; absent elsewhere
    import msvcrt
except ImportError:                      # pragma: no cover
    msvcrt = None
try:                                     # POSIX fallback (the server only ever runs on Windows)
    import fcntl
except ImportError:
    fcntl = None

_UI_FORBIDDEN = 0x1
MAGIC = b"FXDP1\n"                       # marks our sealed blob; plaintext JSON never starts with this

# Windows-only (DPAPI). Guarded so a non-Windows import of the server never crashes here; sealing/unsealing then
# raises if actually used off-Windows (the server only ever runs on Windows).
try:
    import ctypes.wintypes as wt

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _AVAILABLE = True
except Exception:                        # noqa: BLE001  (non-Windows: wintypes/windll absent)
    _AVAILABLE = False


def _in(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _protect(data: bytes) -> bytes:
    if not _AVAILABLE:
        raise RuntimeError("DPAPI is only available on Windows")
    inp, out = _in(data), _BLOB()
    if not _crypt32.CryptProtectData(ctypes.byref(inp), ctypes.c_wchar_p("fixer-cfg"),
                                     None, None, None, _UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def _unprotect(data: bytes) -> bytes:
    if not _AVAILABLE:
        raise RuntimeError("DPAPI is only available on Windows")
    inp, out = _in(data), _BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None,
                                       _UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def is_sealed(path: str | os.PathLike) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def read_text(path: str | os.PathLike) -> str | None:
    """The file's text: decrypted when sealed, returned as-is when it's a legacy plaintext file, None when absent."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return None
    if raw.startswith(MAGIC):
        return _unprotect(raw[len(MAGIC):]).decode("utf-8")
    return raw.decode("utf-8-sig")       # legacy plaintext (tolerate a BOM)


def write_text(path: str | os.PathLike, text: str) -> None:
    """Seal `text` and write it atomically. Verifies the round-trip first, so the file is never left unreadable."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = MAGIC + _protect(text.encode("utf-8"))
    if _unprotect(blob[len(MAGIC):]).decode("utf-8") != text:
        raise RuntimeError("DPAPI round-trip check failed; refusing to write")
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.seal.tmp")   # unique per process AND thread
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, p)
    finally:
        try:
            tmp.unlink()                 # only still there when the write or replace failed
        except OSError:
            pass


class LockTimeout(RuntimeError):
    """Another process (or thread) held the lock for the whole wait."""


class ConfigUnreadable(RuntimeError):
    """The file exists but can't be unsealed or parsed. A writer must stop here: writing an empty document over it
    would wipe what is in it. read_failed is True when the bytes couldn't be read or unsealed at all (this can be
    temporary: a virus scanner or backup holding the file, a brief DPAPI error), False when they were read but are
    not valid (parse error, wrong shape), which only a change to the file can fix."""
    read_failed = False


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()                # lock files this thread already holds (so a nested use does not deadlock)


def _os_lock(fd: int, deadline: float, name: str) -> None:
    if msvcrt is None and fcntl is None:
        return                           # no cross-process lock on this platform: the in-process lock still applies
    delay = 0.01
    while True:
        try:
            if msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)       # byte 0 only; the OS drops it if this process dies
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise LockTimeout(f"{name} is busy (another Fixer process is changing it); try again in a moment.") from None
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def _os_unlock(fd: int) -> None:
    try:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass                             # closing the handle releases it anyway


@contextlib.contextmanager
def file_lock(path: str | os.PathLike, timeout: float = 10.0):
    """Hold an exclusive lock for `path` (a lock file `<name>.lock` beside it) across threads AND processes, for a
    read-modify-write. Waits at most `timeout` seconds, then raises LockTimeout. The OS releases the lock when the
    holder exits or crashes, so there is no stale lock and no pid check. Re-entrant within one thread."""
    p = Path(path)
    lp = p.with_name(p.name + ".lock")
    key = os.path.normcase(os.path.abspath(lp))
    held = getattr(_HELD, "d", None)
    if held is None:
        held = _HELD.d = {}
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    deadline = time.monotonic() + timeout
    with _THREAD_LOCKS_GUARD:
        tl = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if not tl.acquire(timeout=timeout):
        raise LockTimeout(f"{p.name} is busy (another part of Fixer is changing it); try again in a moment.")
    fd = None
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o600)
        _os_lock(fd, deadline, p.name)
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)
            _os_unlock(fd)
    finally:
        if fd is not None:
            os.close(fd)
        tl.release()


def load_json_object(path: str | os.PathLike) -> dict:
    """The file's JSON object ({} when it is absent or empty). Raises ConfigUnreadable when it exists but can't be
    unsealed / parsed or is not an object, so a read-modify-write never starts from a wrong empty document."""
    name = Path(path).name
    try:
        txt = read_text(path)
    except Exception as e:               # noqa: BLE001  (DPAPI failure, unreadable bytes, locked by AV, ...)
        err = ConfigUnreadable(f"{name} can't be read ({e.__class__.__name__}), so it was left unchanged.")
        err.read_failed = True
        raise err from e
    if txt is None or not txt.strip():
        return {}
    try:
        data = json.loads(txt)
    except ValueError as e:
        raise ConfigUnreadable(f"{name} isn't valid ({e.__class__.__name__}), so it was left unchanged.") from e
    if not isinstance(data, dict):
        raise ConfigUnreadable(f"{name} isn't in the expected form, so it was left unchanged.")
    return data


def update_json(path: str | os.PathLike, mutate, timeout: float = 10.0, indent: int | None = 2) -> bool:
    """Locked read-modify-write of a sealed JSON object file. Under file_lock it re-reads the file, calls
    mutate(data) to change it in place, and writes it back sealed. mutate may return False to say "nothing to
    change" (no write). Raises ConfigUnreadable (never writes) if the current file can't be read, LockTimeout if the
    lock stays busy. Returns True when it wrote."""
    with file_lock(path, timeout):
        data = load_json_object(path)
        if mutate(data) is False:
            return False
        write_text(path, json.dumps(data, indent=indent))
        return True


def set_aside(path: str | os.PathLike, timeout: float = 10.0) -> Path | None:
    """The way out of a config that can't be unsealed or parsed (for example after a Windows password reset): rename
    it to `<name>.unreadable-<date>-<time>` beside itself, so the next write starts a fresh file. It is never
    deleted, and nothing is moved unless the file is still unreadable once the lock is held (a read that failed only
    for a moment is tried again first). Returns the new path, or None when the file is absent or can be read."""
    p = Path(path)
    with file_lock(p, timeout):
        if not p.exists():
            return None
        for attempt in range(3):
            try:
                load_json_object(p)
                return None                      # readable: nothing to move
            except ConfigUnreadable as e:
                if not e.read_failed or attempt == 2:
                    break
                time.sleep(0.2)                  # may only be a scanner holding it: look again
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest, n = p.with_name(f"{p.name}.unreadable-{stamp}"), 1
        while dest.exists():
            n += 1
            dest = p.with_name(f"{p.name}.unreadable-{stamp}-{n}")
        os.replace(p, dest)
        return dest


def migrate(path: str | os.PathLike, json_only: bool = True, lock: bool = False) -> str:
    """Upgrade one legacy plaintext config to a sealed blob, in place, only if it round-trips (and, unless
    json_only is False, is valid JSON: a token file is plain text). Returns 'absent', 'already-sealed', or 'sealed'.
    lock=True holds file_lock(path) for the swap (keys.json: pair.py may be writing it). Raises on a real failure
    (caller logs and keeps going)."""
    p = Path(path)
    if not p.exists():
        return "absent"
    if is_sealed(p):
        return "already-sealed"
    if lock:
        with file_lock(p):
            return migrate(p, json_only)         # re-checked inside the lock: another process may have sealed it
    text = p.read_bytes().decode("utf-8-sig")
    if json_only:
        json.loads(text)                 # never seal garbage
    write_text(p, text)                  # verifies round-trip before replacing
    if read_text(p) != text:             # and confirm what's now on disk reads back identically
        raise RuntimeError("post-migrate verify failed")
    return "sealed"
