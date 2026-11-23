"""Fixer Desktop: the PC half of Fixer (chat with a bigger vision model, image make/edit on the GPU).

Chat runs on llama.cpp (llama-server, bundled in ROOT/llama, bound to 127.0.0.1 only) with a GGUF build of the
chosen model; it starts on the first chat and stays loaded. Non-GGUF chat models fall back to transformers.
Images and video run on diffusers (video encoded to H.264 MP4 in memory with PyAV).

Rules for this file (the owner's, 2026-11-16):
  * Nothing about a request is ever written to disk: no uploads folder, no outputs folder, no content logs,
    no history. Requests and results live in memory only and are dropped when the reply is sent.
  * The owner's rules and settings live on the phone and travel with each request.
  * Every request and every reply frame is AES-256-GCM encrypted with that phone's own key (pair.py).
  * Models live in one folder with a size cap; when a new model would push it over, the least recently used
    model is deleted first. The folder never grows past the cap.

Protocol (version "fixer1"):
  POST /v1/<op>   headers X-Fixer-Phone: <phone id>, X-Fixer-Time: <unix seconds>
                  body    nonce(12) | ciphertext | tag(16) of a JSON object; AAD = "fixer1|<op>|<phone>|<time>"
  reply           a stream of frames: uint32 big-endian length | nonce | ciphertext | tag,
                  AAD = "<request AAD>|<frame number>", each a JSON object:
                  {"t":"text","v":...} {"t":"progress","v":0..1} {"t":"status","v":...}
                  {"t":"image","v":<base64 PNG>} {"t":"info",...} {"t":"error","v":...} {"t":"done"}
  11-17 additions (all additive, protocol string unchanged):
  queued          pictures, edits and videos line up. Requests carry "slot" (the place they come from: a new one
                  replaces that place's unfinished job), "label" and "ack"; the first reply frame is
                  {"t":"queued","id","position","kind","slot","label"}, result frames carry "n", a stopped job ends
                  with {"t":"info","stopped":"<reason>"} + an error. Jobs keep going when the phone disconnects; a
                  finished result is held in memory only, up to 30 min, for the phone to collect.
  queue           POST /v1/queue {"action": list | watch | cancel | move | ack, ...}: the line, re-attach, cancel
                  (ids or slot), reorder, acknowledge a collected result. Never waits for the card.
  free_memory     stop everything and unload every model (ComfyUI, the chat model, the helper); reports the figures.
  chat "caps":["queue"] adds the queue tools and a live note of the line to that chat turn.
  11-18 additions:
  leave           POST /v1/leave {}: the phone app went to the background. Replies {"t":"info","leaving":true}, done.
                  Once the line is empty and leave_free_minutes (local.json, default 5; 0 = off) have passed with no
                  other request, the PC frees everything (engine, helper, chat model, ComfyUI). The 20-min idle rule
                  stays as the backstop.
  free_after_min  in "leave" and "warm": the owner's choice of how long after he leaves the PC frees itself (1, 3, 5,
                  10, 20, 60 or 0 = never; any number 0..1440). It replaces leave_free_minutes and stretches (never
                  shortens) the 20-min idle rule; 0 turns both off. Absent = the PC's own default.
  video          "seconds": 1..video_max_seconds (default 30; the phone sends 5, 10, 20 or 30). Longer than 5 s is
                  made as 5-second parts, each continuing from the previous part's last frame; the "video" frame's
                  "seconds" is the real length.
  this PC only    GET /local/state (also "pid", "comfy", "llama", "free_after_min", "comfy_guarded", "llama_keyed"),
                  POST /local/unload,
                  POST /local/quit (needs the header X-Fixer-Local: 1): replies {"quitting": true}, stops every job and child, wipes
                  comfy-private and ends the process (<= 20 s). GET /local/timings: the recent finished jobs' speed
                  numbers (no words). A request carrying an Origin header (a web page in a browser) is refused.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import fnmatch
import gc
import inspect
import io
import json
import re
import logging
import os
import secrets
import shutil
import tempfile
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("FIXER_HOME", r"C:\FixerDesktop"))
MODELS = ROOT / "models"
KEYS = ROOT / "keys.json"
CAP_GB = float(os.environ.get("FIXER_MODEL_CAP_GB", "200"))   # the model folder's size limit
PORT = int(os.environ.get("FIXER_PORT", "8765"))
HOST = os.environ.get("FIXER_HOST", "0.0.0.0")
def _local_config() -> dict:
    """local.json beside the server: this home's addresses (search server, away address). Kept out of the public
    repo; see local.example.json."""
    try:
        return json.loads((Path(__file__).resolve().parent / "local.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


LOCAL = _local_config()


def _tune(key: str, default: float) -> float:
    """11-17: knobs read fresh from local.json (beside the server, never in git), so they can be tuned without a
    code change: brain args apply on the brain's next start, ComfyUI's on its next start, the rest at once."""
    try:
        return float(_local_config().get(key, os.environ.get("FIXER_" + key.upper(), default)))
    except (TypeError, ValueError):
        return default


def _tune_text(key: str, default: str) -> str:
    """Like _tune, for a string knob: local.json (read fresh), else the FIXER_<KEY> environment variable, else default."""
    try:
        v = _local_config().get(key, os.environ.get("FIXER_" + key.upper(), default))
        return str(v) if v not in (None, "") else default
    except Exception:                                    # noqa: BLE001
        return default


SEARCH_URL = os.environ.get("FIXER_SEARCH_URL") or LOCAL.get("search_url", "")   # SearXNG (LAN)
IDLE_UNLOAD = float(os.environ.get("FIXER_IDLE_MINUTES", "20")) * 60   # give the GPU and RAM back after this

# Must be set before huggingface_hub / transformers / diffusers are imported.
import fixer_secure as sec  # noqa: E402  (DPAPI at-rest sealing for keys.json, macros.json, library.json, tokens)

os.environ["HF_HOME"] = str(MODELS)
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["DISABLE_TELEMETRY"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
# Hugging Face read token (for gated models): first of these that exists. `hf auth login` writes the last one.
for _t in (ROOT / "hf_token", MODELS / "token", Path.home() / ".cache" / "huggingface" / "token"):
    if _t.is_file() and not os.environ.get("HF_TOKEN"):
        try:
            os.environ["HF_TOKEN"] = (sec.read_text(_t) or "").strip()   # ROOT/hf_token may be sealed (DPAPI)
        except Exception:                              # noqa: BLE001  (unreadable token: carry on without one)
            pass

from aiohttp import web  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

# Warnings only, to the console; never request content.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fixer")

PROTOCOL = "fixer1"
CLOCK_SKEW = 120          # seconds a request's timestamp may be off
# Qwen3.6 27B, refusals removed with Heretic v2 (MPOA; 6/100 refusals vs 92, MMLU 85.6 vs 86.7). the owner 11-17: the 9B
# softened adult requests and followed instructions loosely. Measured on the 5080 (11-17): Q4_K_M 16.6 GB spills
# (10.6 tok/s); Q3_K_L 14.3 GB with the photo reader in RAM fits the card: 41 tok/s.
# 11-17: Qwen3.6 35B-A3B (mixture of experts, Heretic, MTP head kept). Only ~3B parameters work per token, so the
# expert weights live in system RAM (--cpu-moe) and only attention/shared parts sit on the card (~4-6 GB): ComfyUI
# keeps the rest, and the chat answers while pictures are made.
# 11-17 (slimmer lineup): the dense 27B is gone (its files were deleted); a phone that still asks for it gets the MoE brain.
MOE_BRAIN = "llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-GGUF"
DENSE_BRAIN = "llmfan46/Qwen3.6-27B-uncensored-heretic-v2-GGUF"    # retired: only the alias below (and the checks) use it
DEFAULT_BRAIN = MOE_BRAIN
BRAIN_PINS = {MOE_BRAIN: ("q4_k_s", "bf16")}      # weights quant, projector: the files already downloaded
_MOE_NAME = re.compile(r"\d+(\.\d+)?B-A\d+(\.\d+)?B", re.I)


def _is_moe(repo: str | None) -> bool:
    """A mixture-of-experts brain ("35B-A3B": 35B total, 3B active): its experts run from system RAM."""
    return bool(repo) and (repo in BRAIN_PINS or bool(_MOE_NAME.search(repo)))


PAUSED_TEXT = ("The chat is paused while your computer makes a video: it needs the memory for it. "
               "It's back as soon as the video is done.")
# Older phone builds send the original (safetensors) repo ids; run their GGUF builds on llama.cpp instead.
BRAIN_ALIASES = {
    DENSE_BRAIN: DEFAULT_BRAIN,                  # 11-17: removed from the lineup (files deleted)
    "DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF": DEFAULT_BRAIN,   # 11-17 upgrade
    "huihui-ai/Huihui-Qwen3-VL-8B-Instruct-abliterated": DEFAULT_BRAIN,
    "Qwen/Qwen3-VL-8B-Instruct": "Qwen/Qwen3-VL-8B-Instruct-GGUF",
}
LLAMA_SERVER = ROOT / "llama" / "llama-server.exe"      # llama.cpp b12443, CUDA 13.5 build (README)
GGUF_QUANTS = ("Q5_K_M", "Q4_K_M", "Q6_K", "Q8_0", "Q4_K_S", "Q3_K_L")       # preference order, within the budget below
BRAIN_VRAM_GB = float(os.environ.get("FIXER_BRAIN_VRAM_GB", "15.5"))   # weights + vision projector
BRAIN_CTX = int(os.environ.get("FIXER_BRAIN_CTX", "16384"))
# Picture models that are a single retrained "transformer" file on top of a base diffusers model:
# id -> (base repo, file in the id's repo, diffusers class). Empty now; the name stays (used in the diffusers path).
TRANSFORMER_SWAPS: dict = {}
DEFAULT_VIDEO = "yetter-ai/Wan2.2-TI2V-5B-Turbo-Diffusers"   # 4-step distill; 11-17: only used if already on disk
MAX_BODY = 40 * 1024 * 1024


class UserFacing(RuntimeError):
    """An error whose message is written for the person using the app."""


# ---------------------------------------------------------------- keys and crypto

class Keys:
    """keys.json: {"phones": {"<id>": {"name": "...", "key": "<base64url 32 bytes>", "expires": <unix, optional>}}}.
    Re-read when it changes. A pairing with "expires" (a temporary one, made away from home) stops working then."""

    def __init__(self, path: Path):
        self.path = path
        self.mtime = None
        self.keys: dict[str, bytes] = {}
        self.failed_mtime = None                         # a file version whose bytes couldn't be read (maybe only for a moment)
        self.retry_at = 0.0
        self.warned_mtime = None

    def _load(self, m: float) -> None:
        """Read keys.json for file version m. A file that was read but is invalid counts as no keys for that version
        (cached, read again when the file changes). A file whose bytes couldn't be read at all (a virus scanner or
        backup holding it, a brief DPAPI error) is NOT cached: it is tried 3 times now, and again every 2 s after."""
        err = None
        for attempt in range(3):
            try:
                data = sec.load_json_object(self.path)
                phones = data.get("phones", {})
                keys = {pid: b64d(p["key"]) for pid, p in phones.items()}
                expires = {pid: float(p["expires"]) for pid, p in phones.items() if p.get("expires")}
                # 11-19: a fresh pairing code must be used within 60 s ("pair_by", written by pair.py); the phone's first
                # request confirms it (confirm_pairing removes the deadline). Unused past it: refused, then deleted.
                pair_by = {pid: float(p["pair_by"]) for pid, p in phones.items() if p.get("pair_by")}
                self.keys, self.expires, self.pair_by = keys, expires, pair_by
                self.mtime, self.failed_mtime = m, None
                return
            except Exception as e:                       # noqa: BLE001  (corrupt / truncated / unreadable keys.json)
                err = e
                if not getattr(e, "read_failed", False) or attempt == 2:
                    break
                time.sleep(0.05)
        # No keys for now (every phone is refused). The file is never written from here: the writers refuse to touch a
        # file they can't read, so pairings aren't wiped. Logged once per version of the file.
        if self.warned_mtime != m:
            self.warned_mtime = m
            log.warning("keys.json can't be read, so no phone can connect until it is fixed or reset "
                        "(on the PC: python pair.py --reset keys, then pair each phone again): %s",
                        err if isinstance(err, sec.ConfigUnreadable) else err.__class__.__name__)   # never file content
        self.keys, self.expires, self.pair_by = {}, {}, {}
        if getattr(err, "read_failed", False):
            self.failed_mtime, self.retry_at = m, time.monotonic() + 2.0      # try again soon: it may only be busy
        else:
            self.mtime = m                                                    # invalid contents: wait for the file to change

    def get(self, phone: str) -> bytes | None:
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            self.keys, self.mtime, self.failed_mtime = {}, None, None
            return None
        if m != self.mtime and not (m == self.failed_mtime and time.monotonic() < self.retry_at):
            self._load(m)
        now = time.time()
        if now > getattr(self, "expires", {}).get(phone, float("inf")):
            return None
        if now > getattr(self, "pair_by", {}).get(phone, float("inf")):
            return None
        return self.keys.get(phone)

    def pending(self, phone: str) -> bool:
        """11-19: True while this phone's pairing code hasn't been used yet (it still has a "pair_by")."""
        return phone in getattr(self, "pair_by", {})


def confirm_pairing(phone: str) -> None:
    """11-19: the phone's first authenticated request: the pairing is complete, so its 60 s deadline goes."""
    def confirm(data: dict):
        entry = data.get("phones", {}).get(phone)
        if not entry or "pair_by" not in entry:
            return False                               # nothing to change (checked on the fresh copy, inside the lock)
        entry.pop("pair_by")
        entry["paired_at"] = int(time.time())

    try:
        # 8: read-change-write under the keys.json lock (shared with pair.py and the expiry sweep), from a fresh read;
        # a file that can't be read is left alone. A short wait; the request handler runs this in the executor.
        if sec.update_json(KEYS, confirm, timeout=3.0):    # sealed at rest (DPAPI)
            log.warning("pairing confirmed for phone %s", phone)
    except Exception as e:                             # noqa: BLE001
        log.warning("couldn't confirm a pairing: %s", e.__class__.__name__)


def b64d(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def seal(key: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)


def unseal(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < 28:
        raise ValueError("short")
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad)


class ReplayGuard:
    """Remembers request nonces for the clock-skew window so a captured request can't be sent twice."""

    def __init__(self):
        self.seen: dict[bytes, float] = {}

    def fresh(self, nonce: bytes) -> bool:
        now = time.time()
        if len(self.seen) > 2000:
            self.seen = {n: t for n, t in self.seen.items() if now - t < 2 * CLOCK_SKEW}
        if nonce in self.seen:
            return False
        self.seen[nonce] = now
        return True


# ---------------------------------------------------------------- model folder with a size cap

BRAIN_FILES = ["*.json", "*.safetensors", "*.txt", "*.model", "*.jinja", "*.tiktoken"]
IMAGE_FILES = ["model_index.json", "*/*.json", "*/*.safetensors", "*/*.txt", "*/*.model", "*/*.jinja"]


def wanted(files: list[tuple[str, int]], patterns: list[str]) -> list[tuple[str, int]]:
    picked = [(f, s) for f, s in files if any(fnmatch.fnmatch(f, p) for p in patterns)]
    if patterns is IMAGE_FILES:
        # Diffusers folders sometimes carry both full and half-precision copies; keep one.
        names = {f for f, _ in picked}
        def duplicate(f: str) -> bool:
            if not f.endswith(".safetensors"):
                return False
            if ".fp32." in f:
                return True
            plain = not any(v in f for v in (".fp16.", ".bf16."))
            return plain and (f.replace(".safetensors", ".fp16.safetensors") in names or
                              f.replace(".safetensors", ".bf16.safetensors") in names)
        picked = [(f, s) for f, s in picked if not duplicate(f)]
    return picked


def _complete(path: Path) -> bool:
    """A local copy counts only if it has its weights: for a diffusers model, every part model_index.json lists
    has a folder with files. (Reading just model_index.json, as the edit check does, leaves a snapshot holding
    that one file; on 11-16 Chroma was then "found" locally and failed to load instead of downloading.)"""
    idx = path / "model_index.json"
    if idx.is_file():
        try:
            parts = [k for k, v in json.loads(idx.read_text(encoding="utf-8")).items()
                     if isinstance(v, list) and len(v) == 2 and v[0] is not None]
        except Exception:
            return False
        return all((path / k).is_dir() and any((path / k).iterdir()) and _part_has_weights(path / k) for k in parts)
    return any(path.iterdir())


def _part_has_weights(part: Path) -> bool:
    """A diffusers part that is a model (it has a config.json) counts only once its weights are there too: the small
    json files are linked into the snapshot as they finish, while a big .safetensors is still downloading, so an
    interrupted download leaves a folder with just its config. Parts without a config.json (tokenizer, scheduler)
    hold small files only. With a weights index, every file it names must be present."""
    if not (part / "config.json").is_file():
        return True
    try:
        indexes = list(part.glob("*.index.json"))
        for ix in indexes:
            names = set(json.loads(ix.read_text(encoding="utf-8")).get("weight_map", {}).values())
            if names and all((part / n).is_file() for n in names):
                return True
        if indexes:
            return False
        return any(f.is_file() and f.suffix.lower() in (".safetensors", ".bin", ".pt", ".pth", ".ckpt")
                   for f in part.iterdir())
    except Exception:                                # noqa: BLE001
        return False


def ensure_model(repo: str, patterns: list[str], status, kind: str = "model") -> str:
    """Downloads the files of `repo` matching `patterns` into the capped folder and returns its path."""
    from huggingface_hub import HfApi, snapshot_download

    try:
        path = snapshot_download(repo, allow_patterns=patterns, local_files_only=True)
        if _complete(Path(path)):
            return path
    except Exception:
        pass
    info = HfApi().model_info(repo, files_metadata=True)
    files = wanted([(s.rfilename, s.size or 0) for s in info.siblings], patterns)
    return ensure_files(repo, files, status, kind)


def ensure_files(repo: str, files: list[tuple[str, int]], status, kind: str = "model") -> str:
    """Downloads exactly `files` [(name, size)] of `repo` into the capped folder (evicting least-recently-used
    models first) and returns the snapshot path."""
    from huggingface_hub import scan_cache_dir, snapshot_download

    names = [f for f, _ in files]
    try:
        path = snapshot_download(repo, allow_patterns=names, local_files_only=True)
        if all((Path(path) / n).is_file() for n in names):
            return path
    except Exception:
        pass
    need = sum(s for _, s in files)
    cap = int(CAP_GB * 1024 ** 3)
    if need > cap:
        raise UserFacing(f"This model is too large ({need / 1024 ** 3:.0f} GB; the limit is {CAP_GB:.0f} GB).")
    MODELS.mkdir(parents=True, exist_ok=True)
    # No automatic removal: on 11-16 the oldest-used rule deleted the edit model (FLUX.2 klein 9B, 35 GB), the chat
    # model and the tools to fit a 6.6 GB download. The owner removes models himself (phone: press and hold a model
    # marked "On your computer").
    try:
        cache = scan_cache_dir(MODELS / "hub")
        # Only the requested files that are already on disk come off `used` (`need` counts them); the repo's other
        # files (another quant, a projector) stay counted.
        wanted_names, seen_blobs, here = set(names), set(), 0
        for r in cache.repos:
            if r.repo_id != repo:
                continue
            for rev in r.revisions:
                for f in rev.files:
                    if f.blob_path in seen_blobs:
                        continue
                    if f.file_path.relative_to(rev.snapshot_path).as_posix() in wanted_names:
                        seen_blobs.add(f.blob_path)
                        here += f.size_on_disk
        used = cache.size_on_disk - here
    except Exception:
        used = 0
    used += _tree_size(COMFY_LORAS) + _tree_size(MODELS / "comfy-vae")   # LoRAs and the moved VAE count too
    if used + need > cap:
        def gb(b):
            return max(1, round(b / 1024 ** 3))
        raise UserFacing(f"Not enough room on your computer for this {kind}: it needs {gb(need)} GB and "
                         f"{gb(max(0, cap - used))} of {CAP_GB:.0f} GB are free. Remove a model first: in the model "
                         f"list, press and hold one marked “On your computer”.")
    status(f"Downloading {kind} ({max(1, round(need / 1024 ** 3))} GB)…")
    return snapshot_download(repo, allow_patterns=names)


# ---------------------------------------------------------------- chat on llama.cpp

_GGUF_PICK: dict[str, tuple | None] = {}


def _cached_files(repo: str) -> list[tuple[str, int]]:
    snaps = MODELS / "hub" / ("models--" + repo.replace("/", "--")) / "snapshots"
    out = []
    for snap in snaps.glob("*"):
        for f in snap.rglob("*.gguf"):
            out.append((f.relative_to(snap).as_posix(), f.stat().st_size))
    return out


def gguf_choice(repo: str):
    """For a GGUF repo: (files to download, weights file, vision projector or None). None for other repos."""
    if repo in _GGUF_PICK:
        return _GGUF_PICK[repo]
    from huggingface_hub import HfApi
    try:
        info = HfApi().model_info(repo, files_metadata=True)
        files = [(x.rfilename, x.size or 0) for x in info.siblings]
    except Exception:
        files = _cached_files(repo)          # offline: whatever is already on disk
        if not files:
            raise
    ggufs = [(f, z) for f, z in files if f.lower().endswith(".gguf") and "-of-0" not in f]
    if not ggufs:
        _GGUF_PICK[repo] = None
        return None
    projs = [(f, z) for f, z in ggufs if "mmproj" in f.lower()]
    weights = [(f, z) for f, z in ggufs if "mmproj" not in f.lower()]
    proj = None
    for want in ("q8_0", "f16", "bf16"):
        proj = proj or next((x for x in projs if want in x[0].lower()), None)
    proj = proj or (min(projs, key=lambda x: x[1]) if projs else None)
    pin = BRAIN_PINS.get(repo)
    if pin:
        # 11-17: the MoE file (19 GB) is bigger than the card budget below, which would fall through to the smallest
        # quant and download it; the pinned weights and projector are the files already downloaded.
        pinned = next((x for x in weights if pin[0] in x[0].lower()), None)
        proj = next((x for x in projs if pin[1] in x[0].lower()), proj)
        if pinned:
            choice = ([pinned] + ([proj] if proj else []), pinned[0], proj[0] if proj else None)
            _GGUF_PICK[repo] = choice
            return choice
    if _is_moe(repo):
        budget = _tune("brain_moe_budget_gb", 24) * 1e9      # experts live in RAM: the card budget doesn't apply
    else:
        budget = BRAIN_VRAM_GB * 1e9
    budget -= (proj[1] if proj else 0)
    pick = None
    for q in GGUF_QUANTS:
        pick = pick or next((x for x in weights if q.lower() in x[0].lower() and x[1] <= budget), None)
    pick = pick or min(weights, key=lambda x: x[1])
    choice = ([pick] + ([proj] if proj else []), pick[0], proj[0] if proj else None)
    _GGUF_PICK[repo] = choice
    return choice


class _KillWithMe:
    """A Windows job object: child processes in it die when this server exits, however it exits, so a
    llama-server can never be left holding graphics memory."""

    def __init__(self):
        self.h = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

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

        k32.GetCurrentProcess.restype = ctypes.c_void_p
        h = k32.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if h and k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.h = h
        self._k32 = k32
        self.owned = False

    def own(self) -> bool:
        """11-18: puts this server itself in the job, so EVERY child it starts later (the helper process, anything a
        library spawns) is in it too and dies with the server, however the server dies. Called from main() only, so
        importing this file (the checks) changes nothing. Nested jobs (Windows 8+): the venv launcher and the tray each
        already hold the server in a job of their own; adding it to this new, empty one nests it under those. The job
        never allows breakaway, so a child cannot slip out of it."""
        if not self.h or self.owned:
            return self.owned
        try:
            self.owned = bool(self._k32.AssignProcessToJobObject(self.h, self._k32.GetCurrentProcess()))
        except Exception:                                # noqa: BLE001
            self.owned = False
        return self.owned

    def add(self, proc):
        """A child started before own() (or if own() failed). Popen, or a multiprocessing Process (11-18)."""
        if not self.h:
            return
        handle = getattr(proc, "_handle", None) or getattr(getattr(proc, "_popen", None), "_handle", None)
        if handle:
            try:
                self._k32.AssignProcessToJobObject(self.h, int(handle))
            except Exception:                            # noqa: BLE001
                pass


_JOB = _KillWithMe()


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LlamaBrain:
    """The chat model on llama.cpp's server, listening on 127.0.0.1 only. Started on the first chat and kept
    running (it loads in seconds and holds ~7 GB of graphics memory); restarted when the phone picks another
    model. No logs, no web UI, no saved slots: llama-server writes nothing to disk."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.repo: str | None = None
        self.vision = False
        self.on_gpu = True
        self.lock = threading.RLock()        # held only while the process state changes, never across a download or a wait
        self.start_lock = threading.RLock()  # one start at a time (a second chat waits here, not on `lock`)
        self.gen = 0                         # bumped by stop / pause / stop_for_good: a start that finds it changed gives up
        self.moe = False                     # 11-17: running as the mixture-of-experts brain (experts in RAM)
        self.paused_for: str | None = None   # "video": the brain was stopped to give a video the memory
        self.resume_wanted = False
        self.starting = False
        self.card_gb = 0.0                   # measured card share (display only)
        self.epoch = 0                       # 11-18: bumped by a hard stop (leave / quit): an in-flight resume gives up
        self.key: str | None = None          # 11-18: this start's API key (never logged, never in any report)
        self.keyed: bool | None = None       # 11-18: this start proved the key is required
        self.slots = 1                       # 11-19: llama-server slots of the running start (see _llama_args)

    def key_state(self) -> bool | None:
        """11-18: for /local/state: True = the running server refuses keyless requests (proven at start), False =
        started without a key (llama_key 0), None = not running."""
        return self.keyed if self.running() else None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self, repo: str, choice, status, epoch: int | None = None) -> int:
        """Starts (or keeps) the server for `repo`; returns its port. One start at a time (start_lock); `lock` is held
        only while the process state changes, not across the download or the wait for the model to load, so stop,
        pause and resume never wait minutes behind a start. `epoch`: the resume path gives up if a leave / quit came in.
        11-22: `gen` is read BEFORE queueing on start_lock, so a caller that waited behind another start while the model
        was stopped (free memory, leave, a video) gives up instead of starting it again."""
        asked_at = self.gen
        with self.start_lock:
            with self.lock:
                if epoch is not None and self.epoch != epoch:
                    raise UserFacing("The chat model was stopped before it was ready. Try again.")
                self._cancelled(asked_at)                # a stop / pause / leave came in while this call was queued
                if self.paused_for:
                    raise UserFacing(PAUSED_TEXT)            # chat during a video that took the memory
                moe = _is_moe(repo)
                gpu = moe or not VIDEO_BUSY.is_set()         # the MoE never moves to RAM-only for a video: it keeps its share
                if self.running() and self.repo == repo and (self.on_gpu == gpu or not gpu):
                    return self.port
                gen = self.gen
            return self._start(repo, choice, status, gpu, gen)

    def _cancelled(self, gen: int) -> None:
        """Under `lock`: a stop, pause or leave came in while this start was under way; give up (it has already
        stopped whatever was running)."""
        if self.gen != gen:
            raise UserFacing(PAUSED_TEXT if self.paused_for else "The chat model was stopped before it was ready. Try again.")

    def _stop_if(self, proc) -> None:
        with self.lock:
            if self.proc is proc:
                self._stop()

    def _start(self, repo: str, choice, status, gpu: bool = True, gen: int | None = None) -> int:
        if gen is None:
            gen = self.gen
        self.starting = True
        try:
            files, weights, proj = choice
            # The download takes no lock. The model that is running now is stopped only once the new files are on disk,
            # so a switch that fails (cap, offline) leaves the working chat model alone.
            path = Path(ensure_files(repo, files, status))
            with self.lock:
                self._cancelled(gen)
                self._stop()
                status("Loading model…")
                port = _free_port()
                moe = _is_moe(repo) and gpu
                keyed = _tune("llama_key", 1) >= 1          # 11-18: 0 = the old start (no key, CORS default)
                key = secrets.token_urlsafe(32) if keyed else None
                args = _llama_args(repo, path, weights, proj, gpu, port, keyed=keyed)
                if gpu and not moe and "COMFY" in globals():
                    COMFY.free()          # dense only: the MoE fits beside ComfyUI (ComfyUI starts with --reserve-vram)
                try:
                    before = _gpu_info()[1] if gpu else 0.0
                except Exception:                            # noqa: BLE001
                    before = 0.0
                self.key = key
                proc = self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL, env=_llama_env(key),
                                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                _JOB.add(proc)
            deadline = time.time() + 300                     # the wait for the model to load: no lock held
            while time.time() < deadline:
                if proc.poll() is not None:
                    with self.lock:
                        self._cancelled(gen)
                        if self.proc is proc:
                            self.proc = None
                            self.key = None
                    raise UserFacing("The model couldn't start. Try another model.")
                if _llama_status(port, "GET", "/health", timeout=2) == 200:
                    break
                time.sleep(0.25)
            else:
                self._stop_if(proc)
                raise UserFacing("The model took too long to start.")
            if keyed:
                # 11-18: prove the key is required before anything is sent: a keyless request to a protected endpoint
                # must be refused and a keyed one answered. If not, stop it and refuse to use it (fail closed).
                proof = _llama_key_proof(port, key)
                if proof is not True:
                    log.warning("chat model key check failed (%s); chat model stopped", proof)
                    self._stop_if(proc)
                    raise UserFacing("The model couldn't start. Try another model.")
            with self.lock:
                self._cancelled(gen)                         # a stop during the load has already ended the process
                self.keyed = keyed
                self.slots = int(args[args.index("-np") + 1])     # 11-19: what this start really has
                self.port, self.repo, self.vision, self.on_gpu = port, repo, bool(proj), gpu
                self.moe = moe
                try:
                    self.card_gb = max(0.0, before - _gpu_info()[1]) if gpu else 0.0
                except Exception:                            # noqa: BLE001
                    self.card_gb = 0.0
            return port
        finally:
            self.starting = False

    def stop(self):
        with self.lock:
            self.gen += 1                    # a start under way gives up
            self._stop()

    def _stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.port = self.repo = None
        self.key = self.keyed = None
        self.vision = False
        self.moe = False
        self.card_gb = 0.0                   # (paused_for is NOT cleared here)

    def pause(self, why: str, status) -> None:
        """11-17: a job that needs the RAM the experts hold (a video): stop the brain and refuse chats until resume()."""
        with self.lock:
            # back-to-back videos: the first pause already stopped it; keep "bring it back afterwards"
            self.resume_wanted = self.resume_wanted or self.running() or self.starting   # a start under way counts
            self.gen += 1                    # ...and gives up: the video needs the memory now
            self._stop()
            self.paused_for = why
        status("Pausing the chat for this video: your computer needs the memory. It comes back when the video is done.")

    def resume(self) -> None:
        """Lifts the pause; restarts the brain only if it was running before (an idle brain stays off)."""
        with self.lock:
            if not self.paused_for:
                return
            self.paused_for = None
            again, self.resume_wanted = self.resume_wanted, False
            epoch = self.epoch
        if again:
            choice = gguf_choice(LAST_BRAIN)
            if choice is not None:
                def back():
                    # 11-18: not after a leave-free / quit stopped everything (ensure checks the epoch again under its
                    # own lock). No lock is held across the start: the download must not block a stop.
                    with self.lock:
                        if self.epoch != epoch:
                            return
                    self.ensure(LAST_BRAIN, choice, lambda s: None, epoch)
                _quietly(back)

    def stop_for_good(self) -> None:
        """11-18: the phone left (or the server quits): stop, forget any pause, and never come back by itself."""
        with self.lock:
            self.epoch += 1
            self.gen += 1                    # a start under way gives up
            self.resume_wanted = False
            self.paused_for = None
            self._stop()

    def state(self) -> str:
        return "paused" if self.paused_for else "loading" if self.starting else "ready" if self.running() else "off"


def _llama_env(key: str | None) -> dict | None:
    """11-18: the environment for llama-server only. With a key: a copy of ours plus LLAMA_API_KEY (this build reads
    the key from it, so it is never on the command line where any program on this PC could read it), and no
    LLAMA_ARG_API_KEY_FILE that could replace it. Without one (llama_key 0): None, i.e. inherited, as before."""
    if not key:
        return None
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("LLAMA_API_KEY", "LLAMA_ARG_API_KEY_FILE")}
    env["LLAMA_API_KEY"] = key
    return env


_BRAIN_KEY = object()                                  # "the running brain's key" (the default for _llama_request)


def _llama_request(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                   timeout: float = 600, key=_BRAIN_KEY, hold: list | None = None):
    """11-18: the ONE way Fixer talks to llama-server: every request carries "Authorization: Bearer <key>" (the
    running brain's key unless one is given; key=None sends none, only for the start-up proof). Returns
    (connection, response); the caller reads the response and closes the connection. `hold`: the connected socket is
    appended to it once the request is sent (the connection drops its own reference when the answer says "close"), so
    another thread can end a request that is still waiting for its answer."""
    import http.client
    k = LLAMA.key if key is _BRAIN_KEY else key
    h = dict(headers or {})
    if k:
        h["Authorization"] = f"Bearer {k}"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=h)
        if hold is not None and conn.sock is not None:
            hold.append(conn.sock)
        return conn, conn.getresponse()
    except BaseException:
        conn.close()
        raise


def _llama_status(port: int, method: str, path: str, timeout: float = 5, key=_BRAIN_KEY) -> int:
    """The HTTP status of one small request through _llama_request; 0 when there was no answer."""
    import http.client
    try:
        conn, r = _llama_request(port, method, path, timeout=timeout, key=key)
    except (OSError, http.client.HTTPException):
        return 0
    try:
        r.read()
        return r.status
    except (OSError, http.client.HTTPException):
        return 0
    finally:
        conn.close()


LLAMA_PROOF_PATH = "/props"            # protected by the key in llama-server (only /health and /models stay open)


def _llama_key_proof(port: int, key: str):
    """11-18: True when a keyless request is refused (401/403) and the keyed one answered (200); otherwise a short
    reason for the log line (never the key)."""
    keyless = _llama_status(port, "GET", LLAMA_PROOF_PATH, key=None)
    keyed = _llama_status(port, "GET", LLAMA_PROOF_PATH, key=key)
    if keyless in (401, 403) and keyed == 200:
        return True
    return f"keyless answered {keyless}, keyed {keyed}"


CHAT_SLOT, PROMPT_SLOT = 0, 1            # 11-19: llama-server slots (see _llama_args)


def _brain_slots() -> int:
    """11-19: how many llama-server slots the chat model starts with (local.json "brain_slots", 1..2, default 2)."""
    return 2 if _tune("brain_slots", 2) >= 2 else 1


def _llama_args(repo: str, path: Path, weights: str, proj: str | None, gpu: bool, port: int,
                keyed: bool = False) -> list[str]:
    """The llama-server command line (split out so it can be checked without starting anything). Never the key:
    that goes in the child's environment (_llama_env)."""
    # 11-19: two slots, each with the full context: slot 0 is chat, slot 1 the prompt writer (enhance_prompt). With
    # one shared slot a picture's prompt made the chat wait and wiped its cached conversation (the next reply re-read
    # it all). The second slot's KV cache costs well under 1 GB. local.json "brain_slots": 1 restores the old way.
    slots = _brain_slots()
    args = [str(LLAMA_SERVER), "-m", str(path / weights), "--host", "127.0.0.1", "--port", str(port),
            "-fa", "on", "-c", str(BRAIN_CTX * slots), "-ctk", "q8_0", "-ctv", "q8_0", "-np", str(slots),
            "--no-webui", "--log-disable", "--jinja"]
    if keyed:
        args += ["--cors-origins", "localhost"]   # 11-18: this build's "same-origin only" (default is every site)
    moe = _is_moe(repo) and gpu
    if moe:
        # 11-17: experts in RAM, the rest on the card. --fit stays on (it fills only what is unset: if ComfyUI holds
        # more of the card than expected, fewer layers go up instead of the start failing).
        n = int(_tune("brain_ncmoe", -1))
        args += ["--fit", "on", *(["--cpu-moe"] if n < 0 else ["--n-cpu-moe", str(n)]),
                 "-t", str(int(_tune("brain_threads", 7))), "-b", "2048", "-ub", str(int(_tune("brain_ubatch", 1024)))]
        if _tune("brain_mtp", 0) >= 1:
            args += ["--spec-type", "draft-mtp"]
        if _tune("brain_moe_cache_mib", 0) > 0:
            args += ["--moe-cache-mib", str(int(_tune("brain_moe_cache_mib", 0)))]
    else:
        args += ["--fit", "on"] if gpu else ["-ngl", "0"]       # unchanged dense behaviour
    if proj:
        args += ["--mmproj", str(path / proj)]
        if not moe and (not gpu or (path / weights).stat().st_size > 10 * 1024 ** 3):
            args += ["--no-mmproj-offload"]        # dense-27B rule only: a big model needs the whole card (19.5 -> 41 tok/s)
        if moe and _tune("brain_projector_on_card", 1) < 1:
            args += ["--no-mmproj-offload"]
    if not gpu:
        args += ["--device", "none", "--no-op-offload"]   # not one byte on the card
    return args


LLAMA = LlamaBrain()
VIDEO_BUSY = threading.Event()      # set while a video renders: the chat model must not take graphics memory


def _data_uri(b64: str) -> str:
    head = b64[:8]
    if head.startswith("/9j/"):
        return "data:image/jpeg;base64," + b64
    if head.startswith("iVBOR"):
        return "data:image/png;base64," + b64
    img = _image_in(b64)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _chat_llama_once(p: dict, emit, cancel, port: int):
    import http.client
    msgs: list[dict] = []
    if p.get("instructions"):
        msgs.append({"role": "system", "content": p["instructions"]})
    for m in p.get("messages", []):
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = m.get("text", "")
        if m.get("image") and role == "user" and LLAMA.vision:
            msgs.append({"role": role, "content": [{"type": "image_url", "image_url": {"url": _data_uri(m["image"])}},
                                                   {"type": "text", "text": text}]})
        else:
            msgs.append({"role": role, "content": text})
    think = bool(p.get("think", False))
    last = next((m.get("text", "") for m in reversed(p.get("messages", [])) if m.get("role") != "assistant"), "")
    if think and "\n\nMy message: " in last:
        # A photo turn (the phone puts the photo tools before "My message:"): the brain only routes the change, so
        # Think adds nothing but wait. 11-17: 40 s of thought on "More realistic please", then no answer at all.
        think = False
    temp = 0.6 if think else float(p.get("temperature", 0.7))      # Qwen3.6 card: thinking wants 0.6 / top_p 0.95
    # Qwen's settings for chat without thinking (model cards): top_p 0.8, top_k 20. Thinking is off unless the phone
    # asks for it: a thinking model (Qwen3.5) otherwise spends seconds to tens of seconds reasoning invisibly
    # before every reply, and the reasoning counts against max_tokens (it could cut the picture JSON short).
    # With Think on (11-17) the reasoning streams to the phone as "thought" frames and gets its own token room.
    body = {"messages": msgs, "stream": True, "max_tokens": int(p.get("max_tokens", 1500)) + (8000 if think else 0),
            "temperature": temp, "top_p": 0.95 if think else float(p.get("top_p", 0.8)), "top_k": int(p.get("top_k", 20)),
            "presence_penalty": float(p.get("presence_penalty", 1.5)),   # Qwen3.6 card, non-thinking chat
            "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": think}}
    if isinstance(p.get("_slot"), int) and getattr(LLAMA, "slots", 1) > p["_slot"]:
        body["id_slot"] = p["_slot"]                   # 11-19: chat and the prompt writer each keep their own slot
    conn = None
    said = False
    held: list = []
    done = threading.Event()

    def _watch():
        # 11-22: while the model reads a long prompt or a photo no line arrives, and the read below cannot see a
        # cancel until one does. Closing the socket itself ends that read at once (and a request still waiting for
        # its answer) and tells llama-server to stop; the cancel is then the normal end of the turn. The low-level
        # close is deliberate: socket.close() waits for the response's file object, which the reader is inside of.
        import _socket
        import socket
        while not done.wait(0.5):
            if cancel.is_set() and held:
                try:
                    sock = held[0]
                    sock.shutdown(socket.SHUT_RDWR)
                    _socket.socket.close(sock)
                except Exception:                       # noqa: BLE001
                    pass
                return

    threading.Thread(target=_watch, daemon=True, name="chat-cancel-watch").start()
    try:
        conn, r = _llama_request(port, "POST", "/v1/chat/completions", body=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, timeout=600, hold=held)
        if r.status != 200:
            try:
                msg = json.loads(r.read()).get("error", {}).get("message", "")
            except Exception:
                msg = ""
            if "context" in msg.lower():
                raise UserFacing("This conversation is too long for the model. Start a new chat.")
            raise UserFacing("The model couldn't answer that. Try again.")
        thinking = said = finished = False
        for raw in r:
            if cancel.is_set():
                break                                   # closing the connection stops the generation
            line = raw.strip()
            if line.startswith(b"error:"):              # 11-22: llama-server's own way to report a failure mid-stream
                raise UserFacing("The model hit a problem while answering. Try again.")
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                finished = True
                break
            obj = json.loads(data)
            if obj.get("error"):
                raise UserFacing("The model hit a problem while answering. Try again.")
            if isinstance(obj.get("timings"), dict) and obj["timings"].get("predicted_per_second"):
                LAST_SPEED["tok_s"] = round(float(obj["timings"]["predicted_per_second"]), 1)   # for the "models" look-up
            d = ((obj.get("choices") or [{}])[0] or {}).get("delta", {})
            if d.get("reasoning_content") and think:
                emit({"t": "thought", "v": d["reasoning_content"]})
            delta = d.get("content")
            # A model that writes <think>...</think> inline: that part goes to the phone as thought (Think on) or
            # is hidden (Think off).
            out = ""
            while delta:
                if thinking:
                    if "</think>" in delta:
                        inner, delta = delta.split("</think>", 1)
                        thinking = False
                    else:
                        inner, delta = delta, ""
                    if think and inner:
                        emit({"t": "thought", "v": inner})
                elif "<think>" in delta:
                    pre, delta = delta.split("<think>", 1)
                    out, thinking = out + pre, True
                else:
                    out, delta = out + delta, ""
            if out and not said:
                out = out.lstrip()                     # no blank lines left behind by hidden thinking
            if out:
                said = True
                emit({"t": "text", "v": out})
        if not finished and not said and not think and not cancel.is_set():
            raise UserFacing("The model stopped before it answered. Try again.")   # the stream ended with nothing
    except (ConnectionError, http.client.HTTPException, OSError) as e:
        if isinstance(e, UserFacing):
            raise
        if cancel.is_set():
            return                                      # the cancel ended the read: not an error
        if not LLAMA.running():
            LLAMA.stop()
            raise UserFacing("The model stopped unexpectedly. Try again.")
        raise
    finally:
        done.set()
        if conn is not None:
            conn.close()
    if think and not said and not cancel.is_set():
        # It thought and then said nothing (the phone was left showing "…"): answer again, without thinking.
        _chat_llama_once({**p, "think": False}, emit, cancel, port)


# ---------------------------------------------------------------- the GPU: one model loaded at a time

class Engine:
    """The brain and the image model stay loaded side by side: the brain (4-bit, ~6 GB) lives on the graphics card,
    the image model waits in RAM and moves to the card one part at a time while it draws. Swapping them for each
    other reread tens of GB from disk on every switch between chat and images. Both unload after the idle time."""

    def __init__(self):
        self.brain_repo: str | None = None
        self.model = None
        self.processor = None
        self.image_repo: str | None = None
        self.pipes: dict[str, object] = {}
        self.active = None                      # the pipe that holds the offload hooks
        self.video_repo: str | None = None
        self.vpipes: dict[str, object] = {}
        self.vactive = None
        self.tools: dict[str, object] = {}       # small single-job models: cut-out, upscaler (under 1 GB each)

    @property
    def kind(self) -> bool:
        """Something that the idle timer should unload (the llama.cpp chat model stays)."""
        return bool(self.brain_repo or self.image_repo or self.video_repo or self.tools or _worker_alive())

    @property
    def repo(self) -> str | None:
        names = [r for r in (LLAMA.repo, self.brain_repo, self.image_repo, self.video_repo, _worker_loaded()) if r]
        return " + ".join(names) or None

    def unload_brain(self):
        self.model = self.processor = None
        self.brain_repo = None
        _free()

    def unload_image(self):
        for p in self.pipes.values():
            try:
                p.remove_all_hooks()
            except Exception:
                pass
        self.pipes = {}
        self.active = None
        self.image_repo = None
        _free()

    def unload_video(self):
        for p in self.vpipes.values():
            try:
                p.remove_all_hooks()
            except Exception:
                pass
        self.vpipes = {}
        self.vactive = None
        self.video_repo = None
        _free()

    def unload(self):
        self.unload_brain()
        self.unload_image()
        self.unload_video()
        self.unload_tools()
        if "WORKER" in globals():
            WORKER.stop()

    def unload_tools(self):
        self.tools = {}
        _free()

    def tool(self, repo: str, files: list[str], status, kind: str, load):
        """A small model, loaded once and kept on the card until the idle unload."""
        if repo not in self.tools:
            path = ensure_model(repo, files, status, kind)
            self.tools[repo] = load(path)
        return self.tools[repo]

    # -- video

    def video(self, repo: str, mode: str, status):
        """mode: "t2v" (from a description) or "i2v" (animates a photo)."""
        import torch
        from diffusers import DiffusionPipeline
        if self.video_repo != repo:
            self.unload_video()
            path = ensure_model(repo, IMAGE_FILES, status, "video model")
            status("Loading video model…")
            kw = {"torch_dtype": torch.bfloat16}
            # The text reader stays full precision: a 4-bit UMT5 lost parts of prompts ("waves", "sunset") on 11-16.
            big = _big_components(path)
            if big:
                from diffusers.quantizers import PipelineQuantizationConfig
                kw["quantization_config"] = PipelineQuantizationConfig(
                    quant_backend="bitsandbytes_4bit", components_to_quantize=big,
                    quant_kwargs={"load_in_4bit": True, "bnb_4bit_quant_type": "nf4",
                                  "bnb_4bit_compute_dtype": torch.bfloat16})
            base = DiffusionPipeline.from_pretrained(path, **kw)
            if base.__class__.__name__.startswith("Wan") and "turbo" in repo.lower():
                from diffusers import UniPCMultistepScheduler      # what the 4-step distill was trained with
                base.scheduler = UniPCMultistepScheduler.from_config(base.scheduler.config, flow_shift=5.0)
            if hasattr(base, "vae") and hasattr(base.vae, "enable_tiling"):
                base.vae.enable_tiling()
            # Weights stored in FP8, computed in bf16 (the usual 16 GB-card setup): the 5B transformer drops from
            # ~10 GB to ~5 GB, which is what lets the model run at its trained 1280x704 (832x448 came out cooked).
            tr = getattr(base, "transformer", None)
            if tr is not None and hasattr(tr, "enable_layerwise_casting") and not big:
                tr.enable_layerwise_casting(storage_dtype=torch.float8_e4m3fn, compute_dtype=torch.bfloat16)
            self.vpipes = {"t2v": base}
            self.video_repo = repo
            if "image" in _call_params(base):
                self.vpipes["i2v"] = base
        if mode not in self.vpipes:
            import diffusers
            twin = {"WanPipeline": "WanImageToVideoPipeline", "LTXPipeline": "LTXImageToVideoPipeline"}
            name = twin.get(self.vpipes["t2v"].__class__.__name__)
            if not name:
                raise UserFacing("This video model can't animate photos. Choose another one.")
            t2v = self.vpipes["t2v"]
            if name == "WanImageToVideoPipeline":
                # Built from the same parts, NOT with from_pipe: from_pipe moved every part onto the graphics card
                # at once (14 GB) and photo-to-video always ran out of memory on 11-16.
                t2v.remove_all_hooks()
                self.vactive = None
                self.vpipes[mode] = diffusers.WanImageToVideoPipeline(
                    tokenizer=t2v.tokenizer, text_encoder=t2v.text_encoder, vae=t2v.vae, scheduler=t2v.scheduler,
                    transformer=t2v.transformer, transformer_2=getattr(t2v, "transformer_2", None),
                    boundary_ratio=t2v.config.get("boundary_ratio"),
                    expand_timesteps=bool(t2v.config.get("expand_timesteps", False)))
            else:
                self.vpipes[mode] = getattr(diffusers, name).from_pipe(t2v)
        pipe = self.vpipes[mode]
        if self.vactive is not pipe:
            for other in self.vpipes.values():
                if other is not pipe:
                    other.remove_all_hooks()
            pipe.enable_model_cpu_offload()
            self.vactive = pipe
        return pipe

    # -- chat

    def brain(self, repo: str, status):
        if self.brain_repo == repo:
            return self.model, self.processor
        self.unload_brain()
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor, BitsAndBytesConfig
        path = ensure_model(repo, BRAIN_FILES, status)
        status("Loading model…")
        cfg = json.loads((Path(path) / "config.json").read_text(encoding="utf-8"))
        kw = {"dtype": torch.bfloat16, "device_map": "cuda:0"}
        if "quantization_config" not in cfg and _params_billion(path) > 5:
            kw["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True)
        self.model = AutoModelForImageTextToText.from_pretrained(path, **kw).eval()
        self.processor = AutoProcessor.from_pretrained(path)
        self.brain_repo = repo
        return self.model, self.processor

    # -- images

    def pipe(self, repo: str, mode: str, status):
        """mode: "make" (text to image) or "edit" (takes a starting image)."""
        import torch
        from diffusers import AutoPipelineForImage2Image, DiffusionPipeline
        if self.image_repo != repo:
            self.unload_image()
            overlay = TRANSFORMER_SWAPS.get(repo)
            path = ensure_model(overlay[0] if overlay else repo, IMAGE_FILES, status, "image model")
            kw = {"torch_dtype": torch.bfloat16}
            if overlay:
                # A community retrain of the picture part only: the rest comes from the base model already here.
                base_repo, filename, cls_name = overlay
                from huggingface_hub import HfApi
                size = next((s.size or 0 for s in HfApi().model_info(repo, files_metadata=True).siblings
                             if s.rfilename == filename), 0)
                swap_dir = ensure_files(repo, [(filename, size)], status, "image model add-on")
                status("Loading image model…")
                import diffusers
                tr = getattr(diffusers, cls_name).from_single_file(str(Path(swap_dir) / filename),
                                                                   torch_dtype=torch.bfloat16)
                # 8-bit storage (computed in bf16): near-lossless, and it fits the 16 GB card with room to spare.
                tr.enable_layerwise_casting(storage_dtype=torch.float8_e4m3fn, compute_dtype=torch.bfloat16)
                kw["transformer"] = tr
            status("Loading image model…")
            big = [c for c in _big_components(path) if not (overlay and c == "transformer")]
            if big:
                # Parts too big for the graphics card are squeezed to 4-bit as they load (files stay as they are).
                from diffusers.quantizers import PipelineQuantizationConfig
                kw["quantization_config"] = PipelineQuantizationConfig(
                    quant_backend="bitsandbytes_4bit", components_to_quantize=big,
                    quant_kwargs={"load_in_4bit": True, "bnb_4bit_quant_type": "nf4",
                                  "bnb_4bit_compute_dtype": torch.bfloat16})
            base = DiffusionPipeline.from_pretrained(path, **kw)
            self.pipes = {"make": base}
            self.image_repo = repo
            if "image" in _call_params(base):
                self.pipes["edit"] = base              # edits natively (Kontext, Qwen-Image-Edit…)
        if mode not in self.pipes:
            try:
                self.pipes[mode] = AutoPipelineForImage2Image.from_pipe(self.pipes["make"])
            except Exception as e:
                raise UserFacing("This image model can't edit photos. Choose another one.") from e
        pipe = self.pipes[mode]
        if self.active is not pipe:
            for other in self.pipes.values():
                if other is not pipe:
                    other.remove_all_hooks()
            pipe.enable_model_cpu_offload()
            self.active = pipe
        return pipe


def _worker_alive() -> bool:
    return "WORKER" in globals() and WORKER.alive


def _worker_loaded() -> str | None:
    return WORKER.loaded if "WORKER" in globals() and WORKER.alive else None


def _free():
    gc.collect()
    if "torch" in sys.modules:                         # never start the graphics card just to free nothing
        sys.modules["torch"].cuda.empty_cache()


def _cap_vram(margin_gb: float = 0.5):
    """Lets this process use only the graphics memory that is really free. On Windows the driver otherwise spills an
    overfull card into system RAM without an error and the job crawls (minutes instead of seconds); with the cap it
    fails at once with out-of-memory, which the jobs handle by freeing the chat model and trying again."""
    import torch
    free, total = _card_free()
    usable = torch.cuda.memory_reserved() + free - int(margin_gb * 1024 ** 3)
    torch.cuda.set_per_process_memory_fraction(max(0.2, min(1.0, usable / total)))


def _card_free() -> tuple[int, int]:
    """Free and total graphics memory in bytes: the smaller of torch's and the driver's (nvidia-smi) figure.
    On Windows torch's figure alone ran high: on 11-16 a video kept the chat model's 7.4 GB beside it, the cap let it
    grow, and 6.2 GB spilled into system RAM (the job crawled for minutes; the phone just spun)."""
    import torch
    free, total = torch.cuda.mem_get_info()
    _, smi_free_gb, smi_total_gb = _gpu_info()
    if smi_total_gb > 0:
        free = min(free, int(smi_free_gb * 1024 ** 3))
    return free, total


PICTURE_NEEDS = float(os.environ.get("FIXER_PICTURE_NEEDS_GB", "8")) * 1024 ** 3   # free card memory a picture wants
VRAM_PART_LIMIT = float(os.environ.get("FIXER_VRAM_PART_GB", "11")) * 1024 ** 3


def _big_components(path: str) -> list[str]:
    """Pipeline parts whose weights won't fit on the card next to the rest at bf16."""
    out = []
    for d in Path(path).iterdir():
        if d.is_dir() and d.name in ("transformer", "text_encoder", "text_encoder_2", "unet"):
            if sum(f.stat().st_size for f in d.glob("*.safetensors")) > VRAM_PART_LIMIT:
                out.append(d.name)
    return out


def _params_billion(path: str) -> float:
    total = sum(f.stat().st_size for f in Path(path).glob("*.safetensors"))
    return total / 2e9


def _call_params(pipe) -> set[str]:
    return set(inspect.signature(pipe.__call__).parameters)


ENGINE = Engine()


# ---------------------------------------------------------------- heavy jobs run in a helper process

WORKER_OPS = {"image", "video", "cutout", "upscale", "warm"}


class _RemoteLlama:
    """Inside the helper: what the jobs need to know about the chat model, which lives in the server process.
    stop() asks the server to stop it (to free graphics memory) and waits until it has."""

    repo = None
    vision = False

    def __init__(self, send, acks):
        self._send, self._acks = send, acks
        self._running, self.on_gpu, self.moe = False, False, False

    def running(self) -> bool:
        return self._running

    def stop(self):
        if self._running:
            import queue
            while True:                                  # an ack that came in late for an earlier stop is not this one's
                try:
                    self._acks.get_nowait()
                except queue.Empty:
                    break
            self._send(("ctl", "stop_llama"))
            try:
                self._acks.get(timeout=60)
            except queue.Empty:
                raise UserFacing("Your computer didn't free the chat model's memory in time. Try again.") from None
        self._running = False


def _worker_main(inbox, outbox):
    """The helper process. Pictures, video, cut-out and upscale load their models here, so ending this process gives
    every byte of RAM and graphics memory back to Windows (Python keeps freed memory for itself otherwise: the
    server once held 41 GB after "unloading", and Windows paged it to disk)."""
    import queue
    global LLAMA
    lower_priority()
    lock = threading.Lock()

    def send(m):
        with lock:
            outbox.send(m)

    acks: queue.Queue = queue.Queue()
    LLAMA = _RemoteLlama(send, acks)
    cancel = threading.Event()
    job = None

    def run(op, payload):
        try:
            JOBS[op](payload, lambda obj: send(("frame", obj)), cancel)
            send(("done", ENGINE.repo))
        except BaseException as e:                       # noqa: BLE001 - everything goes back to the server
            send(("error", e.__class__.__name__, str(e)[:500], isinstance(e, UserFacing), ENGINE.repo))

    while True:
        try:
            msg = inbox.recv()
        except (EOFError, OSError):
            return
        if msg[0] == "job":
            _, op, payload, llama = msg
            LLAMA._running, LLAMA.on_gpu, LLAMA.moe = llama
            cancel.clear()
            job = threading.Thread(target=run, args=(op, payload), daemon=True)
            job.start()
        elif msg[0] == "cancel":
            cancel.set()
        elif msg[0] == "ack":
            acks.put(True)


class Worker:
    """The server's side of the helper process: starts it on the first heavy job, forwards jobs and their frames,
    and ends it to unload (or after a job fails on memory, so nothing stays stuck on the graphics card)."""

    def __init__(self):
        self.proc = None
        self.to_worker = self.from_worker = None
        self.loaded: str | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def _start(self):
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        recv_in, send_in = ctx.Pipe(duplex=False)        # server -> helper
        recv_out, send_out = ctx.Pipe(duplex=False)      # helper -> server
        self.proc = ctx.Process(target=_worker_main, args=(recv_in, send_out), daemon=True, name="fixer-worker")
        self.proc.start()
        _JOB.add(self.proc)                              # 11-18: dies with the server even if own() failed
        recv_in.close()
        send_out.close()
        self.to_worker, self.from_worker = send_in, recv_out
        self.loaded = None

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.join(10)
            except Exception:
                pass
        for c in (self.to_worker, self.from_worker):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        self.proc = self.to_worker = self.from_worker = None
        self.loaded = None
        self.family = None

    def run(self, op: str, payload: dict, emit, cancel):
        # Pictures and video each hold 20-30 GB in RAM. Python never hands freed memory back, so switching from one
        # to the other inside the same helper piled up to 51 GB on 11-16. A fresh helper per switch costs nothing
        # extra: the new model has to load either way.
        family = "video" if op == "video" else "image" if op in ("image", "warm") else None
        if family and self.alive and getattr(self, "family", None) not in (None, family):
            self.stop()
        if not self.alive:
            self.stop()
            emit({"t": "status", "v": "Starting the image engine…"})
            self._start()
        if family:
            self.family = family
        cancelled = False
        try:
            self.to_worker.send(("job", op, payload, (LLAMA.running(), getattr(LLAMA, "on_gpu", True),
                                                     getattr(LLAMA, "moe", False))))
            while True:
                if self.from_worker is None or self.to_worker is None:   # stopped meanwhile ("forget" or unload)
                    raise UserFacing("Stopped: the work on your computer was cleared.")
                if cancel.is_set() and not cancelled:
                    self.to_worker.send(("cancel",))
                    cancelled = True
                if not self.from_worker.poll(0.2):
                    if not self.alive:
                        raise UserFacing("The image engine on your computer stopped. Try again.")
                    continue
                msg = self.from_worker.recv()
                kind = msg[0]
                if kind == "frame":
                    emit(msg[1])
                elif kind == "ctl" and msg[1] == "stop_llama":
                    LLAMA.stop()
                    self.to_worker.send(("ack",))
                elif kind == "done":
                    self.loaded = msg[1]
                    return
                elif kind == "error":
                    _, name, text, user_facing, loaded = msg
                    self.loaded = loaded
                    if user_facing:
                        raise UserFacing(text)
                    if "OutOfMemory" in name or "out of memory" in text.lower():
                        self.stop()                      # a failed big job leaves memory behind: start clean next time
                    raise type(name, (RuntimeError,), {})(text)
        except (EOFError, BrokenPipeError, ConnectionResetError, OSError) as e:
            self.stop()
            raise UserFacing("The image engine on your computer stopped. Try again.") from e


WORKER = Worker()


# ---------------------------------------------------------------- jobs

def _image_in(b64: str):
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    img = ImageOps.exif_transpose(img).convert("RGB")
    return img


def _fit(w: int, h: int, longest: int) -> tuple[int, int]:
    s = min(1.0, longest / max(w, h))
    return max(256, int(w * s) // 16 * 16), max(256, int(h * s) // 16 * 16)


def _gpu_info() -> tuple[str, float, float]:
    """Card name, free and total GB, from the driver (no torch here: that alone takes ~0.5 GB of the card)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.free,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.splitlines()[0]
        name, free, total = [x.strip() for x in out.split(",")]
        return name, float(free) / 1024, float(total) / 1024
    except Exception:
        return "graphics card", 0.0, 0.0


def _ram() -> tuple[float, float]:
    """System memory Windows can hand out now (standby included) and the total, in GB. ctypes only (no psutil)."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return 0.0, 0.0
            return st.ullAvailPhys / 1024 ** 3, st.ullTotalPhys / 1024 ** 3
        page = os.sysconf("SC_PAGE_SIZE")
        return os.sysconf("SC_AVPHYS_PAGES") * page / 1024 ** 3, os.sysconf("SC_PHYS_PAGES") * page / 1024 ** 3
    except Exception:                                    # noqa: BLE001
        return 0.0, 0.0


_CARD_CACHE: list = [0.0, ("graphics card", 0.0, 0.0)]


def _card_cached(max_age: float = 2.0) -> tuple[str, float, float]:
    """_gpu_info() at most every 2 s (the Queue screen polls; nvidia-smi takes ~0.1-0.3 s). Call off the event loop."""
    now = time.monotonic()
    if now - _CARD_CACHE[0] > max_age:
        _CARD_CACHE[1] = _gpu_info()
        _CARD_CACHE[0] = now
    return _CARD_CACHE[1]


def _figures() -> dict:
    """How full the graphics card and the computer's memory are (GB, one decimal). Off the event loop only."""
    name, free, total = _card_cached()
    rfree, rtotal = _ram()
    return {"card": {"name": name, "used_gb": round(max(0.0, total - free), 1), "total_gb": round(total, 1)},
            "memory": {"used_gb": round(max(0.0, rtotal - rfree), 1), "total_gb": round(rtotal, 1)}}


def job_status(p: dict, emit, cancel):
    from huggingface_hub import scan_cache_dir
    try:
        cache = scan_cache_dir(MODELS / "hub")
        used, repos = cache.size_on_disk, sorted(r.repo_id for r in cache.repos)
        # Only models that are really there (a model_index.json fetched by the edit check is not a model).
        sizes = {r.repo_id: r.size_on_disk for r in cache.repos if r.size_on_disk > 50 * 1024 ** 2}
    except Exception:
        used, repos, sizes = 0, [], {}
    used += _tree_size(COMFY_LORAS) + _tree_size(MODELS / "comfy-vae")   # LoRAs and the moved VAE count too
    for alias, real in BRAIN_ALIASES.items():          # the phone may know a chat model by its alias
        if real in sizes:
            sizes[alias] = sizes[real]
    for phone_id, rid in COMFY_RECIPES.items():        # picture models live as ComfyUI files under other names
        r = CREATE_RECIPES.get(rid)
        if phone_id not in sizes and r and _recipe_ready(r):
            sizes[phone_id] = sum(sizes.get(n, 0) for n in r["needs"]) or 1
    if WAN14_REPO in sizes and WAN_TE_REPO in sizes:
        sizes[WAN14_REPO] += sizes[WAN_TE_REPO]
    name, free_gb, total_gb = _gpu_info()
    ram_free, ram_total = _ram()
    emit({"t": "info", "gpu": name, "vram_free_gb": round(free_gb, 1),
          "vram_total_gb": round(total_gb, 1), "loaded": ENGINE.repo, "models": repos, "model_sizes": sizes,
          "models_gb": round(used / 1024 ** 3, 1), "cap_gb": CAP_GB, "protocol": PROTOCOL,
          "ram_free_gb": round(ram_free, 1), "ram_total_gb": round(ram_total, 1),
          "queue_waiting": len(QUEUE.waiting()), "queue_running": 1 if QUEUE.busy() else 0})


def _clean_model_name(repo: str) -> str:
    """A readable fallback name from a repo id (the phone has its own names for the built-ins)."""
    n = repo.split("/")[-1]
    for junk in ("-GGUF", "-gguf", "_ComfyUI_Repackaged", "_ComfyUI_repackaged", "_repackaged", "-bf16", "-fp16"):
        n = n.replace(junk, "")
    return n.replace("-", " ").replace("_", " ").strip() or repo


def _installed_models() -> list[dict]:
    """One entry per model on this computer: {id (the phone id), name, kind (picture/video/chat/other), repos (the
    hub repos it owns), size (bytes), in_use}. A recipe's parts are grouped under its phone id; a part shared with
    another installed model is never listed as its own model."""
    from huggingface_hub import scan_cache_dir
    try:
        sizes = {r.repo_id: r.size_on_disk for r in scan_cache_dir(MODELS / "hub").repos}
    except Exception:                                    # noqa: BLE001
        sizes = {}
    present = set(sizes)
    out: list[dict] = []
    claimed: set[str] = set()
    comfy_loaded = COMFY.running()
    for phone_id, rid in COMFY_RECIPES.items():          # picture models: parts grouped under the phone id
        r = CREATE_RECIPES.get(rid)
        if not r or not _recipe_ready(r):
            continue
        repos = [n for n in r["needs"] if n in present]
        size = sum(sizes.get(n, 0) for n in repos) + (_tree_size(COMFY_VAE) if rid == "chroma" else 0)
        claimed.update(repos)
        out.append({"id": phone_id, "name": r["name"], "kind": "picture", "repos": repos, "size": size,
                    "in_use": comfy_loaded and rid in COMFY.warmed})
    if WAN14_REPO in present:                             # video: the three Wan repos are one model
        vrepos = [x for x in (WAN14_REPO, WAN_TE_REPO, WAN_EXPLICIT_REPO) if x in present]
        claimed.update(vrepos)
        out.append({"id": WAN14_REPO, "name": "Wan 2.2 14B video", "kind": "video", "repos": vrepos,
                    "size": sum(sizes.get(x, 0) for x in vrepos), "in_use": VIDEO_BUSY.is_set()})
    for repo in sorted(present - claimed):               # chat brains (GGUF weights)
        if any(f.lower().endswith(".gguf") for f, _ in _cached_files(repo)):
            out.append({"id": repo, "name": _clean_model_name(repo), "kind": "chat", "repos": [repo],
                        "size": sizes[repo], "in_use": LLAMA.running() and LLAMA.repo == repo})
            claimed.add(repo)
    for repo in sorted(present - claimed):               # editors, tools and anything else left
        out.append({"id": repo, "name": _clean_model_name(repo), "kind": "other", "repos": [repo],
                    "size": sizes[repo], "in_use": repo in (ENGINE.repo or "")})
    return out


def job_installed(p: dict, emit, cancel):
    """Every model on this computer the phone's Models screen can remove, one entry per model."""
    emit({"t": "info", "installed": [{"id": m["id"], "name": m["name"], "kind": m["kind"],
                                      "size_gb": round(m["size"] / 1024 ** 3, 1), "in_use": m["in_use"]}
                                     for m in _installed_models()]})


def job_remove_model(p: dict, emit, cancel):
    """Removes one model from the capped folder (the phone's Models screen). Resolves a phone id to the repos the
    model owns, deletes only repos no other installed model still needs, stops ComfyUI first (Windows won't delete
    open files) and unloads the chat model / helper if they held it. Picking it again later downloads it again."""
    from huggingface_hub import scan_cache_dir
    want = p.get("model") or ""
    want_resolved = BRAIN_ALIASES.get(want, want)
    models = _installed_models()
    entry = next((m for m in models if want in (m["id"],) or want_resolved in ([m["id"]] + m["repos"])), None)
    if entry is None:
        raise UserFacing("That model isn't on your computer.")
    if entry["kind"] == "chat" and LLAMA.running() and LLAMA.repo in entry["repos"]:
        raise UserFacing("That chat model is in use right now. Switch to another chat model first, then remove it.")
    if entry["kind"] == "picture" and sum(1 for m in models if m["kind"] == "picture") <= 1:
        raise UserFacing("That's the only picture model on your computer. Add another before removing this one.")
    keep = {rp for m in models if m is not entry for rp in m["repos"]}   # repos another installed model still needs
    to_delete = [rp for rp in entry["repos"] if rp not in keep]
    COMFY.stop()                                         # release ComfyUI's file handles before deleting (Windows)
    COMFY.warmed.clear()
    for rp in to_delete:
        if LLAMA.running() and LLAMA.repo == rp:
            LLAMA.stop()
        if rp in (ENGINE.repo or ""):
            ENGINE.unload()                              # also ends the helper process
    cache = scan_cache_dir(MODELS / "hub")
    freed = 0
    for rp in to_delete:
        hit = next((r for r in cache.repos if r.repo_id == rp), None)
        if hit is None:
            continue
        freed += hit.size_on_disk
        cache.delete_revisions(*[rev.commit_hash for rev in hit.revisions]).execute()
    left = (scan_cache_dir(MODELS / "hub").size_on_disk
            + _tree_size(COMFY_LORAS) + _tree_size(MODELS / "comfy-vae"))
    emit({"t": "info", "removed": entry["id"], "name": entry["name"], "freed_gb": round(freed / 1024 ** 3, 1),
          "models_gb": round(left / 1024 ** 3, 1), "cap_gb": CAP_GB})


_OLD_DEFAULT_PERSONALITY_START = "You are Fixer, my photo-and-everything assistant."
# 11-17: the owner "hated the way he says who he is" and wanted "a personal assistant to take care of anything I
# need, or some cool shit". Both texts are live-tunable in phone_config.json (default_personality, about_identity).
NEW_DEFAULT_PERSONALITY = (
    "You're Fixer, my personal assistant: whatever I throw at you, you handle it. Easygoing, sharp, a little cocky "
    "in the good way, like the friend who just gets things done. Talk like a person: short, direct, some swagger, a "
    "dry joke when it fits, never corporate and never stiff. Answer what I asked first, at the length it deserves: "
    "a line for simple things, more when it matters. Use lists or steps only when they help. Don't pad, don't "
    "repeat the question back, don't close with a menu of things you could do, and don't talk about yourself, your "
    "tools or how you work unless I ask. Give real opinions when asked, say when you're unsure, and match my mood.")
# Every default the phone has shipped (its Rules.swift text). The server swaps any of them for the live one, so the
# owner gets a new default without an app update; text he wrote himself never matches and is never touched.
_PREVIOUS_DEFAULT_PERSONALITY = (
    "You're a helpful, thoughtful assistant. Talk naturally, like a smart friend: warm, direct and clear. Answer what "
    "was asked first, at the length it deserves: brief for simple things, thorough when it matters. Use lists or "
    "steps only when they help. Don't pad, don't repeat the question back, don't end by listing things you could do, "
    "and don't talk about yourself, your tools or how you work unless asked. Give real opinions when asked, say "
    "when you're unsure, and match my tone.")
ABOUT_IDENTITY_DEFAULT = (
    "Who you are: Fixer, the owner's personal assistant. You take care of whatever he needs: answers, plans, "
    "writing, research, photos, new pictures, video, and running the work on his computer. You're confident and "
    "laid-back with a bit of swagger, quick and sharp, and you never sound corporate or like a help desk. If he asks "
    "who you are, say so in a sentence or two in your own voice, then get on with it. Never bring up where you run, "
    "how messages travel, privacy, encryption, clouds or hardware unless he asks about exactly that, and then use "
    "the look-up tool for the real details.")
LOOKUP_TOOLS_DEFAULT = "\n".join([
    "You can look things up. When I ask for specifics about the models you use (which ones, files, sizes, speed, what",
    "is loaded right now), about my computer (graphics card, memory, storage, versions, what's running) or about my",
    "phone, never guess and never recite from memory: reply with ONE JSON object and nothing else:",
    "{\"tool\": \"lookup\", \"what\": [\"models\", \"computer\", \"phone\", \"jobs\"]}   (list only what the question needs)",
    "models = every chat, picture, edit and video model and what's loaded now; computer = the PC's hardware and",
    "software; phone = my phone's model, iOS, storage and battery; jobs = the work lined up on the computer.",
    "The facts come back as the next message. Then answer from them in plain words, with the exact figures and as",
    "much detail as I asked for. Don't mention the look-up itself.",
])
_ABOUT_BLOCK = re.compile(r"About you \((?:facts|only when asked).*?(?:\n\n|\Z)", re.S)


def _phone_cfg() -> dict:
    try:
        return json.loads(PHONE_CONFIG.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}


def _identity_text() -> str:
    return (_phone_cfg().get("about_identity") or ABOUT_IDENTITY_DEFAULT).strip()


def _without_meta(p: dict) -> dict:
    """The phone attaches an "About you" block (where it runs, the encryption...) to every turn, and models recited it
    ("I'm an AI assistant running directly on your home computer...", the owner 11-17: "I hate the way he says who
    he is"). The server swaps that block for the live identity text, and swaps any shipped default personality for
    the live default; his own written personality is left alone."""
    text = p.get("instructions") or ""
    live = (_phone_cfg().get("default_personality") or NEW_DEFAULT_PERSONALITY).strip()
    if live == _PREVIOUS_DEFAULT_PERSONALITY.strip():
        live = NEW_DEFAULT_PERSONALITY      # a live file still holding the old shipped text counts as unset
    if text.startswith(_OLD_DEFAULT_PERSONALITY_START):
        # The first shipped default steered every chat toward pictures; it ran to the first blank line.
        end = text.find("\n\n")
        text = live + (text[end:] if end >= 0 else "")
    else:
        for shipped in (_PREVIOUS_DEFAULT_PERSONALITY, NEW_DEFAULT_PERSONALITY):
            if shipped != live and text.startswith(shipped):
                text = live + text[len(shipped):]
                break
    ident = _identity_text()
    if _ABOUT_BLOCK.search(text):
        text = _ABOUT_BLOCK.sub(lambda m: ident + "\n\n", text, count=1)
    else:
        text = (text + "\n\n" + ident) if text.strip() else ident
    text = text.strip()
    return p if text == (p.get("instructions") or "") else {**p, "instructions": text}


LAST_BRAIN = DEFAULT_BRAIN        # the chat model the phone last used; an app-open warm-up loads this one
LAST_SPEED: dict = {}             # the brain's last measured speed (tok/s), kept in memory only


# ---------------------------------------------------------------- the look-up tool (11-17)
# The brain answers {"tool": "lookup", "what": [...]}; the PC gathers the facts live (nothing stored), adds them as a
# message and the brain answers again. The JSON never reaches the phone: text that starts like a look-up is held back.

LOOKUP_ROUNDS = 2
LOOKUP_FAIL_TEXT = "I couldn't pull that up just now. Ask me again in a moment."
_LOOKUP_HEAD = '{"tool":"lookup"'
_TOPIC_WORDS = {"model": "models", "models": "models", "brain": "models", "ai": "models", "llm": "models",
                "computer": "computer", "pc": "computer", "gpu": "computer", "hardware": "computer",
                "system": "computer", "desktop": "computer", "machine": "computer",
                "phone": "phone", "iphone": "phone", "device": "phone",
                "job": "jobs", "jobs": "jobs", "queue": "jobs", "line": "jobs"}


def _lookup_topics(what) -> list[str]:
    items = [what] if isinstance(what, str) else list(what) if isinstance(what, (list, tuple)) else []
    out: list[str] = []
    for w in items:
        t = _TOPIC_WORDS.get(str(w).strip().lower())
        if t and t not in out:
            out.append(t)
    return out or ["models", "computer", "phone", "jobs"]


def _parse_lookup(raw: str) -> dict | None:
    """The look-up request in `raw`, or None when it isn't one."""
    s = (raw or "").strip()
    try:
        obj = json.loads(s)
    except ValueError:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s)
        except ValueError:
            return None
    if isinstance(obj, dict) and str(obj.get("tool", "")).lower() == "lookup":
        return {"what": _lookup_topics(obj.get("what"))}
    return None


class _LookupGate:
    """Sits between the model's stream and the phone. Text that starts with {"tool": "lookup" is held back until the
    reply ends; any other text (plain answers, photo and queue JSON) is released as soon as it can't be one, in the
    same pieces as before. Other frames (status, thought) pass straight through."""

    def __init__(self, emit, allow: bool = True):
        self.emit, self.allow = emit, allow
        self.buf = ""
        self.mode: str | None = None          # None = undecided, "pass", "hold"
        self.raw = ""                         # what a held reply said

    def frame(self, f: dict):
        if f.get("t") != "text":
            self.emit(f)
            return
        v = f.get("v") or ""
        if self.mode == "pass":
            self.emit(f)
            return
        self.buf += v
        if self.mode is None:
            s = self.buf.lstrip()
            if not s:
                return
            fenced = s.startswith("```")
            if fenced:
                # A reply that opens with a code fence (```json): wait for the end of the fence line, then judge what
                # follows it. A look-up request inside the fence is held and run like a bare one.
                nl = s.find("\n")
                if nl < 0:
                    return
                s = s[nl + 1:].lstrip()
                if not s:
                    return
            if not s.startswith("{"):
                self._release()
                return
            compact = re.sub(r"\s+", "", s)
            if compact.startswith(_LOOKUP_HEAD):
                self.mode = "hold"
                if fenced:
                    self.buf = s                      # the fence is not part of the request
            elif not _LOOKUP_HEAD.startswith(compact):
                self._release()

    def _release(self):
        self.mode = "pass"
        b, self.buf = self.buf, ""
        if b:
            self.emit({"t": "text", "v": b})

    def finish(self) -> dict | None:
        """Call when the reply is over: the look-up it asked for, or None (anything held is then released)."""
        if self.mode == "hold":
            self.raw, self.buf = self.buf, ""
            req = _parse_lookup(self.raw) if self.allow else None
            if req is None:
                self.emit({"t": "text", "v": LOOKUP_FAIL_TEXT})
            return req
        if self.mode is None and self.buf.strip():
            self._release()
        return None


def _lookup_message(facts: str) -> str:
    return ("(Look-up result, added automatically, not part of my message. Answer my question from it in plain words "
            "with the exact figures, no JSON, and don't mention the look-up.)\n" + facts)


def _chat_llama(p: dict, emit, cancel, port: int):
    """One answer, with up to LOOKUP_ROUNDS look-ups on the way (each: the brain asks, the PC gathers, it answers again)."""
    msgs = list(p.get("messages") or [])
    for rnd in range(LOOKUP_ROUNDS + 1):
        gate = _LookupGate(emit, allow=rnd < LOOKUP_ROUNDS)
        _chat_llama_once({**p, "messages": msgs}, gate.frame, cancel, port)
        req = gate.finish()
        if req is None or cancel.is_set():
            return
        emit({"t": "status", "v": "Looking that up…"})
        try:
            facts = lookup_facts(req["what"], p)
        except Exception:                                # noqa: BLE001
            facts = "The look-up failed; say so briefly and don't guess."
        msgs = msgs + [{"role": "assistant", "text": gate.raw.strip()},
                       {"role": "user", "text": _lookup_message(facts)}]


def _run_text(args: list[str], timeout: float = 8) -> str:
    """A command's output as text ("" on any failure). Its own function so the checks can fake it."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception:                                    # noqa: BLE001
        return ""


def _safe(fn) -> bool:
    try:
        return bool(fn())
    except Exception:                                    # noqa: BLE001
        return False


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def _tree_size(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _proc_ram_gb(pid: int | None) -> float | None:
    """Working-set memory of one process in GB (Windows, ctypes only)."""
    if not pid or sys.platform != "win32":
        return None
    try:
        import ctypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None
        try:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if not k.K32GetProcessMemoryInfo(ctypes.c_void_p(h), ctypes.byref(pmc), pmc.cb):
                return None
            return pmc.WorkingSetSize / 1024 ** 3
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
    except Exception:                                    # noqa: BLE001
        return None


def _card_by_process() -> dict[int, float]:
    """pid -> graphics memory in GB, for what the driver reports (empty when it won't say per process)."""
    out: dict[int, float] = {}
    for line in _run_text(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]).splitlines():
        try:
            pid, mb = [x.strip() for x in line.split(",")[:2]]
            out[int(pid)] = float(mb) / 1024
        except ValueError:
            continue
    return out


def _models_facts() -> str:
    L: list[str] = []
    by_pid = _card_by_process()

    def share(pid) -> str:
        bits = []
        if pid and pid in by_pid:
            bits.append(f"{by_pid[pid]:.1f} GB on the card")
        ram = _proc_ram_gb(pid)
        if ram is not None:
            bits.append(f"{ram:.1f} GB in memory")
        return ", ".join(bits)

    # chat brain
    repo = LLAMA.repo if LLAMA.running() else None
    shown = repo or LAST_BRAIN
    L.append("CHAT BRAIN")
    choice = _GGUF_PICK.get(shown)
    files = dict(_cached_files(shown)) if shown else {}
    wfile = choice[1] if choice else next((f for f in files if "mmproj" not in f.lower()), None)
    pfile = choice[2] if choice else next((f for f in files if "mmproj" in f.lower()), None)
    qm = re.search(r"(IQ\d_\w+|Q\d_K_\w+|Q\d_\d|Q\d_K|BF16|F16)", wfile or "", re.I)
    L.append(f"- model: {shown}")
    if wfile:
        L.append(f"- weights file: {wfile} ({_gb(files[wfile]) if wfile in files else 'size unknown'}, "
                 f"quantisation {qm.group(1).upper() if qm else 'unknown'})")
    if pfile:
        L.append(f"- photo reader (vision projector): {pfile} ({_gb(files[pfile]) if pfile in files else 'size unknown'})")
    L.append(f"- context window: {BRAIN_CTX} tokens, cache type q8_0")
    if repo:
        L.append(f"- state: {LLAMA.state()}; sees photos: {'yes' if LLAMA.vision else 'no'}; "
                 f"{'mixture of experts, expert weights kept in system memory, the rest on the card' if LLAMA.moe else 'dense, whole model on the card' if LLAMA.on_gpu else 'running on the processor only'}")
        sh = share(LLAMA.proc.pid if LLAMA.proc else None)
        L.append(f"- holds: {sh or ('about %.1f GB on the card' % LLAMA.card_gb if LLAMA.card_gb else 'figures not available')}")
    else:
        L.append(f"- state: {LLAMA.state()} (not loaded right now)")
    if LAST_SPEED.get("tok_s"):
        L.append(f"- last measured speed: {LAST_SPEED['tok_s']} tokens per second")
    else:
        L.append("- measured speed: none yet this session")
    # pictures and edits
    L.append("PICTURE AND EDIT MODELS (recipes on this computer; ready = files are downloaded)")
    for rid, r in CREATE_RECIPES.items():
        try:
            ready = _recipe_ready(r)
        except Exception:                                # noqa: BLE001
            ready = False
        d = r.get("defaults", {})
        size = sum(_tree_size(_snapshot(n)) for n in r.get("needs", []))
        L.append(f"- {r['name']} (id {rid}, {'ready' if ready else 'NOT downloaded'}): repos {', '.join(r.get('needs', []))}"
                 f"{'; file ' + r['file'][1] if r.get('file') else ''}; {d.get('steps', '?')} steps, guidance {d.get('cfg', '?')}, "
                 f"sampler {d.get('sampler', '?')}; all its parts {_gb(size)} on disk (some parts are shared with other recipes); {r.get('note', '')}")
    lora = _qwen21_lora()
    L.append(f"- photo edits: Qwen-Image-Edit 2511 Lightning ({QWEN_EDIT_REPO}, 4 steps) for ordinary edits; "
             f"Qwen-Rapid-AIO NSFW ({QWEN_AIO_REPO}/{QWEN_AIO_DIR}) for explicit ones; text reader and VAE from {QWEN_BASE_REPO}. "
             f"On this computer: {'switched on' if QWEN_EDIT_ON else 'switched off'}. {QWEN21_NAME} also edits when chosen "
             f"(the same model as its pictures, so nothing reloads between a picture and its edit): "
             f"{'ready' if _safe(_qwen21_ready) else 'not all downloaded'}")
    L.append(f"- explicit add-on for {QWEN21_NAME}: {_tune_text('qwen21_explicit_lora', QWEN21_LORA_FILE)} "
             f"({'on this computer' if lora else 'not downloaded'}"
             f"{', strength %g' % lora[1] if lora else ''}); loaded only when asked for, never changes your words")
    L.extend(_measured_speed_facts())
    L.append("VIDEO")
    L.append(f"- Wan 2.2 14B image-to-video, 4-step Lightning, fp8 ({WAN14_REPO}), text reader from {WAN_TE_REPO}, "
             f"{WAN14_AREA} pixels per frame at {WAN14_FPS} fps; explicit-motion add-on {WAN_EXPLICIT_REPO}; "
             f"files {'downloaded' if _safe(_wan14_ready) else 'not all downloaded'}; fallback {DEFAULT_VIDEO}")
    L.append("TOOLS")
    L.append(f"- cut-out: {CUT_GENERAL} (objects) and {CUT_SOFT} (people, fur, hair); upscaler: {UPSCALER[0]} / {UPSCALER[1]}, "
             f"longest side up to {UPSCALE_MAX}px")
    # loaded now
    L.append("LOADED RIGHT NOW")
    L.append(f"- chat brain: {'yes' if repo else 'no'}")
    comfy = COMFY.running()
    cs = share(COMFY.proc.pid) if comfy and COMFY.proc else ""
    L.append(f"- picture/video engine (ComfyUI): {'running' if comfy else 'not running'}{': ' + cs if cs else ''}"
             f"{'; models stay loaded until idle unload' if comfy else ''}")
    other = ENGINE.repo if ENGINE.repo and ENGINE.repo != LLAMA.repo else None
    wrk = _worker_loaded()
    L.append(f"- helper process: {wrk or 'none loaded'}{'; other: ' + other if other and other != wrk else ''}")
    f = _figures()
    L.append(f"- graphics card in use overall: {f['card']['used_gb']} of {f['card']['total_gb']} GB; "
             f"computer memory in use: {f['memory']['used_gb']} of {f['memory']['total_gb']} GB")
    try:
        from huggingface_hub import scan_cache_dir
        L.append(f"- downloaded model folder: {_gb(scan_cache_dir(MODELS / 'hub').size_on_disk)} of the {CAP_GB:g} GB allowed")
    except Exception:                                    # noqa: BLE001
        L.append(f"- downloaded model folder: size not available (limit {CAP_GB:g} GB)")
    return "\n".join(L)


def _computer_facts() -> str:
    import platform
    import shutil
    L = ["COMPUTER"]
    name, free_gb, total_gb = _card_cached()
    drv = _run_text(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]).splitlines()
    L.append(f"- graphics card: {name}{', driver ' + drv[0].strip() if drv else ''}; memory {max(0.0, total_gb - free_gb):.1f} of {total_gb:.1f} GB in use")
    cpu = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except Exception:                                    # noqa: BLE001
        cpu = platform.processor()
    L.append(f"- processor: {cpu or 'unknown'}, {os.cpu_count()} threads")
    rfree, rtotal = _ram()
    L.append(f"- memory: {max(0.0, rtotal - rfree):.1f} of {rtotal:.1f} GB in use ({rfree:.1f} GB available)")
    try:
        du = shutil.disk_usage(ROOT)
        L.append(f"- disk holding Fixer's files: {_gb(du.free)} free of {_gb(du.total)}")
    except OSError:
        pass
    L.append(f"- Windows: {platform.platform()} (build {platform.version()})")
    try:
        if sys.platform == "win32":
            import ctypes
            tick = ctypes.windll.kernel32.GetTickCount64
            tick.restype = ctypes.c_ulonglong            # 64-bit: ctypes reads it as a 32-bit int by default (negative after 24.8 days)
            ms = tick()
            L.append(f"- up for {ms // 3600000} h {ms // 60000 % 60} min")
    except Exception:                                    # noqa: BLE001
        pass
    ll = _run_text([str(LLAMA_SERVER), "--version"]) if LLAMA_SERVER.is_file() else ""
    ll = next((x for x in ll.splitlines() if "version" in x.lower()), ll.splitlines()[0] if ll else "")
    L.append(f"- llama.cpp (chat engine): {ll.strip() or 'version not available'}")
    cv = ""
    try:
        m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", (COMFY_DIR / "comfyui_version.py").read_text(encoding="utf-8"))
        cv = m.group(1) if m else ""
    except Exception:                                    # noqa: BLE001
        pass
    L.append(f"- ComfyUI (picture and video engine): {cv or 'version not available'}; Python {platform.python_version()}")
    run = []
    if LLAMA.running():
        run.append("chat brain")
    if COMFY.running():
        run.append("ComfyUI")
    if _worker_alive():
        run.append("helper process")
    if VIDEO_BUSY.is_set():
        run.append("a video render")
    L.append(f"- running: {', '.join(run) or 'nothing of Fixer'}; chat state {LLAMA.state()}")
    return "\n".join(L)


def _phone_facts(device) -> str:
    if not isinstance(device, dict) or not device:
        return ("PHONE\n- This version of the app doesn't send phone details to the computer yet. Tell the owner "
                "the next app update adds it.")
    labels = [("model", "phone model"), ("ios", "iOS"), ("storage_free_gb", "free storage (GB)"),
              ("storage_total_gb", "total storage (GB)"), ("battery", "battery level"),
              ("battery_state", "battery state"), ("low_power", "low power mode"), ("app_version", "app version"),
              ("app_build", "app build"), ("thermal", "temperature state")]
    L = ["PHONE (sent by the app with this message)"]
    seen = set()
    for k, label in labels:
        if k in device:
            v = device[k]
            if k == "battery" and isinstance(v, (int, float)) and 0 <= v <= 1:
                v = f"{round(v * 100)}%"
            L.append(f"- {label}: {v}")
            seen.add(k)
    for k, v in device.items():
        if k not in seen:
            L.append(f"- {k}: {v}")
    return "\n".join(L)


def _jobs_facts(p: dict) -> str:
    note = _queue_note(p.get("_queue") or [], p.get("slot"), _figures, LLAMA.paused_for is not None)
    out = "JOBS\n" + (note or "- nothing is running, waiting or finished on the computer")
    recent = _recent_jobs_facts(10)
    return out + ("\n" + "\n".join(recent) if recent else "")


def lookup_facts(topics: list[str], p: dict) -> str:
    """The facts for a look-up, gathered live and kept nowhere. A topic that fails says so instead of guessing."""
    parts = []
    for t in topics:
        try:
            parts.append(_models_facts() if t == "models" else _computer_facts() if t == "computer"
                         else _phone_facts(p.get("device")) if t == "phone" else _jobs_facts(p))
        except Exception:                                # noqa: BLE001
            parts.append(t.upper() + "\n- couldn't be read right now")
    return "\n\n".join(parts)


def job_chat(p: dict, emit, cancel):
    global LAST_BRAIN
    p = _without_meta(p)
    repo = p.get("model") or DEFAULT_BRAIN
    repo = BRAIN_ALIASES.get(repo, repo)
    LAST_BRAIN = repo
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    choice = gguf_choice(repo)
    if LLAMA.paused_for and choice is not None:
        eta = p.get("_eta_s")
        emit({"t": "info", "brain_paused": LLAMA.paused_for, "eta_s": eta})
        when = f"in about {max(1, round(eta / 60))} min" if eta else "in a few minutes"
        raise UserFacing(PAUSED_TEXT[:-1] + f", {when}.")
    if "queue" in (p.get("caps") or []):
        p = _with_queue(p)
    if choice is not None:
        if ENGINE.brain_repo:
            ENGINE.unload_brain()                      # a transformers fallback model, no longer needed
        tools = _phone_cfg().get("lookup_tools") or LOOKUP_TOOLS_DEFAULT
        p = {**p, "instructions": ((p.get("instructions") or "") + "\n\n" + tools).strip()}
        port = LLAMA.ensure(repo, choice, status)
        return _chat_llama({**p, "_slot": CHAT_SLOT}, emit, cancel, port)
    with TRANSFORMERS_CHAT:                            # the fallback shares the card with image jobs
        _chat_transformers(repo, p, emit, cancel, status)


QUEUE_TOOLS_DEFAULT = "\n".join([
    "You can also manage the work on my computer: the pictures, photo edits and videos lined up or being made there.",
    "When my message ends with a \"(Live status from my computer ...)\" note, it lists every job with its id; without a",
    "note, nothing is running, waiting or finished there. If I ask to stop, cancel, drop, reorder, hurry or clear that",
    "work, or to free the computer's memory, reply with ONE JSON object and nothing else:",
    "{\"say\": \"one short sentence in your own voice on what you did\", \"actions\": [ ... ]}",
    "Actions (as many as needed, in order):",
    "{\"tool\": \"queue_cancel\", \"ids\": [\"a1b2c3\", ...]}   stop these jobs (running or waiting) or drop finished ones",
    "{\"tool\": \"queue_move\", \"id\": \"a1b2c3\", \"to\": 1}    move a waiting job to that place in line (1 = next)",
    "{\"tool\": \"stop_all\"}                                stop everything running or waiting on the computer",
    "{\"tool\": \"free_memory\"}                             stop everything and unload every model from the computer's memory",
    "Use only ids from the latest note. These can go in the same actions list as picture or video actions. Don't",
    "mention the note, ids or memory figures unless I ask what's running or how busy the computer is.",
])


def _with_queue(p: dict) -> dict:
    """11-17: the brain can manage the computer's line (cancel, reorder, stop, free memory). The tool text goes in
    the system prompt (static, so llama's prompt cache keeps it); the live state goes at the END of my latest
    message, so the cached conversation before it is reused (a changing system prompt would re-read all of it)."""
    try:
        tools = json.loads(PHONE_CONFIG.read_text(encoding="utf-8")).get("queue_tools") or QUEUE_TOOLS_DEFAULT
    except Exception:                                    # noqa: BLE001
        tools = QUEUE_TOOLS_DEFAULT
    p = {**p, "instructions": ((p.get("instructions") or "") + "\n\n" + tools).strip()}
    note = _queue_note(p.get("_queue") or [], p.get("slot"), _figures, LLAMA.paused_for is not None)
    msgs = list(p.get("messages") or [])
    if note and msgs and msgs[-1].get("role") != "assistant":
        msgs[-1] = {**msgs[-1], "text": (msgs[-1].get("text") or "") + "\n\n" + note}
        p["messages"] = msgs
    return p


_KIND_WORDS = {"picture": "picture", "pictures": "pictures", "edit": "photo edit", "change": "change", "video": "video",
               "song": "song", "album": "album", "artwork": "artwork", "cutout": "cut-out", "upscale": "sharpen and enlarge", "remove": "removing a model"}


def _queue_note(jobs: list[dict], slot: str | None, figures=None, paused: bool = False) -> str:
    """Empty when nothing is running, waiting or held (the tool text says: no note = nothing there)."""
    if not jobs:
        return ""
    lines = ["(Live status from my computer, added automatically, not part of my message:"]
    for j in jobs:
        js = j.get("slot") or ""
        origin = ("from this chat" if slot and js == slot else "from another chat" if js.startswith("chat:")
                  else "from Make" if js.startswith("make:") else "from the phone")
        word = _KIND_WORDS.get(j.get("kind"), "job")
        head = f'- job {j.get("id")}: {word} "{j.get("label") or ""}"'
        st = j.get("state")
        if st == "running":
            bits = ["being made now"]
            if j.get("progress") is not None:
                bits.append(f"{round(j['progress'] * 100)}% done")
            if j.get("eta_s"):
                bits.append(f"about {max(1, round(j['eta_s'] / 60))} min left")
            lines.append(f"{head}, {', '.join(bits)}, {origin}")
        elif st == "waiting":
            lines.append(f"{head}, waiting, {_ordinal(j.get('position') or 1)} in line, {origin}")
        elif st == "ready":
            lines.append(f"{head}, finished, waiting for my phone to collect it, {origin}")
        else:
            lines.append(f"{head}, didn't work")
    try:
        f = figures() if callable(figures) else (figures or {})
    except Exception:                                    # noqa: BLE001
        f = {}
    card, mem = f.get("card"), f.get("memory")
    tail = []
    if card:
        tail.append(f"Graphics card memory: {card['used_gb']} of {card['total_gb']} GB in use.")
    if mem:
        tail.append(f"Computer memory: {mem['used_gb']} of {mem['total_gb']} GB in use.")
    tail.append(f"The chat is paused: {'yes' if paused else 'no'}.")
    lines.append(" ".join(tail) + ")")
    return "\n".join(lines)


TRANSFORMERS_CHAT = threading.Lock()


def _chat_transformers(repo: str, p: dict, emit, cancel, status):
    """Fallback for chat models without a GGUF build."""
    import torch
    from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer

    LLAMA.stop()
    model, processor = ENGINE.brain(repo, status)
    msgs = []
    if p.get("instructions"):
        msgs.append({"role": "system", "content": [{"type": "text", "text": p["instructions"]}]})
    for m in p.get("messages", []):
        content = []
        if m.get("image"):
            img = _image_in(m["image"])
            img.thumbnail((1280, 1280))
            content.append({"type": "image", "image": img})
        content.append({"type": "text", "text": m.get("text", "")})
        msgs.append({"role": "assistant" if m.get("role") == "assistant" else "user", "content": content})
    inputs = processor.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True, return_dict=True,
                                           return_tensors="pt").to(model.device)

    class Stop(StoppingCriteria):
        def __call__(self, *a, **k):
            return cancel.is_set()

    streamer = TextIteratorStreamer(processor.tokenizer, skip_prompt=True, skip_special_tokens=True)
    temp = float(p.get("temperature", 0.6))
    kw = dict(**inputs, streamer=streamer, max_new_tokens=int(p.get("max_tokens", 1500)),
              stopping_criteria=StoppingCriteriaList([Stop()]), do_sample=temp > 0)
    if temp > 0:
        kw.update(temperature=temp, top_p=0.95)
    failure: list[BaseException] = []

    def run():
        try:
            with torch.inference_mode():
                model.generate(**kw)
        except BaseException as e:   # surfaced after the stream ends
            failure.append(e)
            streamer.end()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for piece in streamer:
        if piece:
            emit({"t": "text", "v": piece})
    t.join()
    del inputs, kw
    if failure:
        raise failure[0]


def _local_complete(repo: str | None) -> bool:
    """11-17 (slimmer lineup): the helper path only runs models that are already on this computer. It never downloads
    (the old default, FLUX.2 klein, was removed, and nothing may fetch a removed model again)."""
    if not repo:
        return False
    snap = _snapshot(TRANSFORMER_SWAPS.get(repo, (repo,))[0])
    return bool(snap is not None and _complete(snap) and (repo not in TRANSFORMER_SWAPS or _snapshot(repo) is not None))


NO_ENGINE_PICTURES = "Pictures need the picture engine on your computer, and it isn't running."
NO_ENGINE_VIDEOS = "Videos need the picture engine on your computer, and it isn't running."


def job_image(p: dict, emit, cancel):
    import torch
    mode = "edit" if p.get("image") else "make"
    repo = p.get("model")
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    if not _local_complete(repo):
        raise UserFacing(NO_ENGINE_PICTURES)            # only reached when ComfyUI can't run; never downloads
    if ENGINE.video_repo:
        ENGINE.unload_video()
    pipe = ENGINE.pipe(repo, mode, status)
    params = _call_params(pipe)
    if mode == "edit" and "image" not in params:
        raise UserFacing(NO_ENGINE_PICTURES)            # a make-only model can't follow an edit
    steps = max(1, int(p.get("steps", 4)))
    guidance = p.get("guidance")
    family = TRANSFORMER_SWAPS.get(repo, (repo,))[0].lower()   # an add-on uses its base model's settings
    if "chroma1-hd" in family:
        steps, guidance = 40, 3.0                      # model card: 40 steps, guidance 3
        if not p.get("negative"):                      # the card's quality-only negative (nothing about content)
            p = {**p, "negative": "low quality, ugly, unfinished, out of focus, deformed, disfigure, blurry, "
                                  "smudged, restricted palette, flat colors"}
    elif pipe.__class__.__name__ == "StableDiffusionXLPipeline":
        # An SDXL-family pipeline: the community's standard settings.
        steps, guidance = 30, 4.5
        if pipe.scheduler.__class__.__name__ != "DPMSolverMultistepScheduler":
            from diffusers import DPMSolverMultistepScheduler     # "DPM++ 2M Karras"
            pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config, use_karras_sigmas=True)
        if not p.get("negative"):                      # quality-only (nothing about content)
            p = {**p, "negative": "lowres, low quality, worst quality, blurry, jpeg artifacts, deformed, bad anatomy, "
                                  "extra fingers, watermark, text, signature"}
    seed = int(p.get("seed") or int.from_bytes(os.urandom(4), "big"))
    kw: dict = {"prompt": p.get("prompt") or "high quality photo", "num_inference_steps": steps,
                "generator": torch.Generator("cpu").manual_seed(seed)}
    if guidance is not None and "guidance_scale" in params:
        kw["guidance_scale"] = float(guidance)
    if p.get("negative") and "negative_prompt" in params:
        kw["negative_prompt"] = p["negative"]
    if mode == "edit":
        start = _image_in(p["image"])
        w, h = _fit(*start.size, int(p.get("longest", 1024)))
        start = start.resize((w, h))
        kw["image"] = [start] if "image_reference" not in params and _takes_list(pipe) else start
        if "strength" in params:
            kw["strength"] = float(p.get("strength", 0.6))
    else:
        w, h = _fit(int(p.get("width", 1024)), int(p.get("height", 1024)), int(p.get("longest", 1024)))
    kw["width"], kw["height"] = w, h

    def on_step(pipe_, i, t, cb_kw):
        emit({"t": "progress", "v": round((i + 1) / steps, 3)})
        if cancel.is_set():
            pipe_._interrupt = True
        return cb_kw

    if "callback_on_step_end" in params:
        kw["callback_on_step_end"] = on_step
    # The chat model steps aside BEFORE drawing when the card is short, instead of after a failed attempt (an
    # out-of-memory during the run wasted the attempt, and one during loading ended the helper).
    if LLAMA.running() and LLAMA.on_gpu and not getattr(LLAMA, "moe", False) and _card_free()[0] < PICTURE_NEEDS:
        emit({"t": "status", "v": "Freeing memory…"})
        LLAMA.stop()
        _free()
    _cap_vram()
    try:
        with torch.inference_mode():
            out = pipe(**kw).images[0]
    except torch.OutOfMemoryError:
        if ENGINE.brain_repo is None and not LLAMA.running():
            raise
        emit({"t": "status", "v": "Freeing memory…"})
        ENGINE.unload_brain()
        LLAMA.stop()                                   # restarts in seconds on the next chat
        _free()
        _cap_vram()
        kw["generator"] = torch.Generator("cpu").manual_seed(seed)
        with torch.inference_mode():
            out = pipe(**kw).images[0]
    if cancel.is_set():
        return
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    emit({"t": "image", "v": base64.b64encode(buf.getvalue()).decode(), "seed": seed})


# Fast decoder for Wan 2.2 5B videos: the official decoder spends ~26 of a 42 s clip turning the result into frames.
TINY_DECODER = ("lightx2v/Autoencoders", "lighttaew2_2_v2.safetensors")   # Apache-2.0, 46 MB
FAST_DECODE = os.environ.get("FIXER_FAST_DECODE", "1") == "1"


def _tiny_decode(latents, status, vae_cfg, parallel: bool = True):
    """Normalised Wan 2.2 latents (C, T, H, W) -> frames (T, H, W, 3) in 0..1, with the tiny decoder.

    The pipeline hands back latents normalised by the VAE's per-channel mean/std; the tiny decoder wants them raw,
    like the full VAE. Fed normalised latents it made every clip over-sharpened and over-saturated (PSNR vs the
    photo 15.1 dB, raw 28.2 dB, full VAE 30.8 dB, measured 11-16)."""
    import torch

    def load(path):
        from taehv import TAEHV
        status("Loading the fast video decoder…")
        return TAEHV(checkpoint_path=str(Path(path) / TINY_DECODER[1])).to("cuda", torch.float16).eval()
    tae = ENGINE.tool(TINY_DECODER[0], [TINY_DECODER[1]], status, "fast video decoder", load)
    z = latents.to("cuda", torch.float32)
    mean = torch.tensor(vae_cfg.latents_mean, device="cuda").view(-1, 1, 1, 1)
    std = torch.tensor(vae_cfg.latents_std, device="cuda").view(-1, 1, 1, 1)
    z = z * std + mean                                                              # back to the VAE's own scale
    x = z.unsqueeze(0).permute(0, 2, 1, 3, 4).half()                                # NCTHW -> NTCHW
    with torch.inference_mode():
        frames = tae.decode_video(x, parallel=parallel, show_progress_bar=False)[0]   # T, C, H, W
    return frames.permute(0, 2, 3, 1).float().cpu().numpy()




class _Mp4Writer:
    """H.264 / yuv420p MP4 (plays on iPhone), built in memory: never a temp file. 11-18: frames go in one at a time,
    so a long clip never needs all of its frames decoded at once (481 frames at 832x480 would be ~0.6 GB)."""

    def __init__(self, fps: int):
        self.fps = fps
        self.buf = io.BytesIO()
        self.out = self.st = None
        self.w = self.h = 0
        self.n = 0

    def add(self, frame) -> None:
        import av
        import numpy as np
        fr = np.asarray(frame)
        if fr.dtype != np.uint8:
            fr = (np.clip(fr, 0, 1) * 255).round().astype(np.uint8)
        if self.out is None:
            self.h, self.w = fr.shape[0] // 2 * 2, fr.shape[1] // 2 * 2
            self.out = av.open(self.buf, mode="w", format="mp4")   # no +faststart: it re-opens the output
            self.st = self.out.add_stream("libx264", rate=self.fps, options={"crf": "19", "preset": "medium"})
            self.st.width, self.st.height, self.st.pix_fmt = self.w, self.h, "yuv420p"
        for pkt in self.st.encode(av.VideoFrame.from_ndarray(np.ascontiguousarray(fr[:self.h, :self.w, :3]),
                                                             format="rgb24")):
            self.out.mux(pkt)
        self.n += 1

    def finish(self) -> bytes:
        if self.out is None:
            return b""
        for pkt in self.st.encode():
            self.out.mux(pkt)
        self.out.close()
        self.out = None
        data = self.buf.getvalue()
        self.buf = io.BytesIO()
        return data

    def discard(self) -> None:
        """A stopped or failed clip: nothing is kept."""
        try:
            if self.out is not None:
                self.out.close()
        except Exception:                                # noqa: BLE001
            pass
        self.out = None
        self.buf = io.BytesIO()


def _encode_mp4(frames, fps: int) -> bytes:
    """H.264 / yuv420p MP4 of a whole array or list of frames (see _Mp4Writer)."""
    wr = _Mp4Writer(fps)
    for fr in frames:
        wr.add(fr)
    return wr.finish()


def _asked_seconds(p: dict) -> float:
    """The clip length the phone asked for (5 when missing or unreadable)."""
    try:
        return float(p.get("seconds", 5))
    except (TypeError, ValueError):
        return 5.0


def job_video(p: dict, emit, cancel):
    import torch
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    mode = "i2v" if p.get("image") else "t2v"
    repo = p.get("model") or DEFAULT_VIDEO
    if repo == WAN14_REPO:
        repo = DEFAULT_VIDEO                           # only reached when ComfyUI can't run: the old 5B path
    if not _local_complete(repo):
        raise UserFacing(NO_ENGINE_VIDEOS)             # 11-17: never downloads a removed or missing model
    if _asked_seconds(p) > 10:                         # 11-18: _video_run caps this path at 10 s; say so plainly
        status("This computer can only make a 10-second video this way.")
    if ENGINE.image_repo:
        ENGINE.unload_image()                          # video needs the RAM and graphics memory
    VIDEO_BUSY.set()
    try:
        _video_run(p, emit, cancel, status, mode, repo)
    finally:
        VIDEO_BUSY.clear()
        if LLAMA.running() and not LLAMA.on_gpu:
            LLAMA.stop()                               # a chat during the video ran from RAM; next one is fast again


def _video_run(p: dict, emit, cancel, status, mode: str, repo: str):
    import torch
    pipe = ENGINE.video(repo, mode, status)
    params = _call_params(pipe)
    wan = pipe.__class__.__name__.startswith("Wan")
    fps = 24
    turbo = "turbo" in repo.lower() or "distill" in repo.lower()
    steps = max(1, int(p.get("steps") or (4 if turbo else (40 if not wan else 30))))
    guidance = p.get("guidance")
    if guidance is None:
        guidance = 1.0 if turbo else (5.0 if wan else 3.0)
    seconds = max(1.0, min(10.0, _asked_seconds(p)))   # the same tolerant reading job_video uses
    mult = 4 if wan else 8                             # frame counts must be k*mult + 1
    frames = int(round(seconds * fps / mult)) * mult + 1
    longest = int(p.get("longest", 1280))           # Wan 2.2 5B is trained at 1280x704; smaller came out cooked
    seed = int(p.get("seed") or int.from_bytes(os.urandom(4), "big"))
    kw: dict = {"prompt": p.get("prompt") or "a cinematic shot", "num_inference_steps": steps,
                "num_frames": frames, "guidance_scale": float(guidance), "output_type": "np",
                "generator": torch.Generator("cpu").manual_seed(seed)}
    if "frame_rate" in params:
        kw["frame_rate"] = fps
    fast = bool(p.get("fast_decode", FAST_DECODE)) and wan and getattr(pipe.vae.config, "z_dim", 0) == 48
    if fast:
        kw["output_type"] = "latent"                   # decoded below with the tiny decoder
    if p.get("negative") and "negative_prompt" in params:
        kw["negative_prompt"] = p["negative"]

    def dims(w: int, h: int) -> tuple[int, int]:
        sc = longest / max(w, h)
        return max(256, int(w * sc) // 32 * 32), max(256, int(h * sc) // 32 * 32)

    def by_area(w: int, h: int) -> tuple[int, int]:
        # Model card: a photo keeps its shape at the trained pixel count (1280*704), sides rounded to /32.
        # Sizing by the longest side made a square photo 1280x1280, ~1.8x the area, and ran out of memory.
        area = 1280 * 704 * (longest / 1280) ** 2
        ar = h / w
        return (max(256, int(round((area / ar) ** 0.5)) // 32 * 32),
                max(256, int(round((area * ar) ** 0.5)) // 32 * 32))

    if mode == "i2v":
        start = _image_in(p["image"])
        w, h = by_area(*start.size)
        kw["image"] = start.resize((w, h))
    else:
        w, h = dims(16, 9) if p.get("aspect") != "portrait" else dims(9, 16)
    kw["width"], kw["height"] = w, h

    # A video needs the whole card (peak ~10.7 GB plus the 11.4 GB text reader passing through), so the chat model
    # always steps aside first; a chat meanwhile answers from RAM, and the next one after the video is on the card
    # again. Sharing the card on 11-16 spilled 6.2 GB into system RAM and the clip never finished.
    if LLAMA.running() and LLAMA.on_gpu:
        status("Freeing memory…")
        LLAMA.stop()
        _free()

    def on_step(pipe_, i, t, cb_kw):
        emit({"t": "progress", "v": round(min(0.95, (i + 1) / steps * 0.95), 3)})
        if i + 1 == steps:
            status("Finishing video…")
        if cancel.is_set():
            pipe_._interrupt = True
        return cb_kw

    if "callback_on_step_end" in params:
        kw["callback_on_step_end"] = on_step
    status("Creating video…")
    _cap_vram()
    try:
        with torch.inference_mode():
            out = pipe(**kw).frames[0]
    except torch.OutOfMemoryError:
        if not LLAMA.running():
            raise
        status("Freeing memory…")
        LLAMA.stop()
        _free()
        _cap_vram()
        kw["generator"] = torch.Generator("cpu").manual_seed(seed)
        with torch.inference_mode():
            out = pipe(**kw).frames[0]
    if cancel.is_set():
        return
    if fast:
        status("Finishing video…")
        short = False
        try:
            out = _tiny_decode(out, status, pipe.vae.config)
        except torch.OutOfMemoryError:
            short = True                               # (retried below, once the failed attempt's memory is released)
        if short:
            _free()
            out = _tiny_decode(out, status, pipe.vae.config, parallel=False)   # frame by frame: slower, far less memory
    mp4 = _encode_mp4(out, fps)
    del out
    emit({"t": "progress", "v": 1.0})
    emit({"t": "video", "v": base64.b64encode(mp4).decode(), "seconds": round((frames - 1) / fps, 2),
          "width": w, "height": h, "seed": seed})


# ---------------------------------------------------------------- cut out (BiRefNet) and upscale (spandrel)

CUT_GENERAL = "ZhengPeng7/BiRefNet_HR"            # hard-edged objects (MIT)
CUT_SOFT = "ZhengPeng7/BiRefNet_HR-matting"       # people, pets, hair and fur: soft edges (MIT)
SOFT_WORDS = ("person", "people", "man", "woman", "boy", "girl", "child", "kid", "baby", "face", "hair", "dog",
              "cat", "pet", "animal", "puppy", "kitten", "bird", "horse", "fur", "me", "him", "her", "them", "fox",
              "wolf", "bear", "lion", "tiger", "rabbit", "bunny", "hamster", "cow", "sheep", "goat", "deer")
UPSCALER = ("Kim2091/UltraSharpV2", "4x-UltraSharpV2.safetensors")   # photo 4x (CC BY-NC-SA: personal use)
UPSCALE_MAX = 8192                                   # longest side of an upscaled photo


def _birefnet(repo: str, status):
    import torch

    def load(path):
        from transformers import AutoModelForImageSegmentation
        status("Loading the cut-out tool…")
        m = AutoModelForImageSegmentation.from_pretrained(path, trust_remote_code=True)
        return m.to("cuda").half().eval()
    return ENGINE.tool(repo, ["*.json", "*.py", "model.safetensors"], status, "cut-out tool", load), torch


def _matte(model, torch, img, size: int = 2048):
    """Alpha (0-255, PIL "L") of the main subject of `img`, at img's size."""
    import numpy as np
    from PIL import Image
    x = np.asarray(img.resize((size, size), Image.BICUBIC), dtype=np.float32) / 255.0
    x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    t = torch.from_numpy(x.transpose(2, 0, 1)).unsqueeze(0).to("cuda").half()
    with torch.inference_mode():
        pred = model(t)[-1].sigmoid()[0, 0].float().cpu().numpy()
    return Image.fromarray((pred * 255).clip(0, 255).astype(np.uint8)).resize(img.size, Image.BICUBIC)


def job_cutout(p: dict, emit, cancel):
    """Mask of what to keep. `boxes` (0-1, [x1, y1, x2, y2]) limit it to those things; none = the main subject."""
    import numpy as np
    from PIL import Image
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    img = _image_in(p["image"])
    labels = " ".join(p.get("labels") or []).lower()
    soft = not labels or any(w in labels.split() for w in SOFT_WORDS)
    model, torch = _birefnet(CUT_SOFT if soft else CUT_GENERAL, status)
    status("Cutting out…")
    W, H = img.size
    boxes = [b for b in (p.get("boxes") or []) if isinstance(b, list) and len(b) == 4]
    if not boxes:
        mask = _matte(model, torch, img)
    else:
        mask = Image.new("L", (W, H), 0)
        for x1, y1, x2, y2 in boxes:
            if cancel.is_set():
                return
            bw, bh = (x2 - x1) * W, (y2 - y1) * H
            pad = 0.15 * max(bw, bh)                    # a little context around each box
            l, t = max(0, int(x1 * W - pad)), max(0, int(y1 * H - pad))
            r, b = min(W, int(x2 * W + pad)), min(H, int(y2 * H + pad))
            if r - l < 8 or b - t < 8:
                continue
            m = _matte(model, torch, img.crop((l, t, r, b)), size=1024)
            region = mask.crop((l, t, r, b))
            mask.paste(Image.fromarray(np.maximum(np.asarray(region), np.asarray(m))), (l, t))
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    emit({"t": "mask", "v": base64.b64encode(buf.getvalue()).decode()})


def job_upscale(p: dict, emit, cancel):
    """4x with a photo upscaler, in overlapping tiles so any photo size fits on the card. JPEG back."""
    import numpy as np
    import torch
    from PIL import Image
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    img = _image_in(p["image"])
    longest = int(p.get("longest", UPSCALE_MAX))

    def load(path):
        from spandrel import ModelLoader
        status("Loading the upscaler…")
        d = ModelLoader().load_from_file(str(Path(path) / UPSCALER[1]))
        dtype = torch.bfloat16 if d.supports_bfloat16 else torch.half if d.supports_half else torch.float32
        return d.to("cuda").to(dtype).eval(), dtype
    model, dtype = ENGINE.tool(UPSCALER[0], [UPSCALER[1]], status, "upscaler", load)
    scale = int(model.scale)
    # Shrink first if 4x would overshoot the size limit by a lot: less work, same final size.
    pre = min(1.0, longest / (scale * max(img.size)))
    if pre < 0.999:
        img = img.resize((max(1, round(img.width * pre)), max(1, round(img.height * pre))), Image.LANCZOS)
    x = torch.from_numpy(np.asarray(img, dtype=np.float32) / 255.0).permute(2, 0, 1).unsqueeze(0)
    _, _, h, w = x.shape
    tile, ov = 512, 32
    out = torch.zeros((3, h * scale, w * scale), dtype=torch.float32)
    weight = torch.zeros((1, h * scale, w * scale), dtype=torch.float32)
    ys = list(range(0, max(1, h - ov), tile - ov)) or [0]
    xs = list(range(0, max(1, w - ov), tile - ov)) or [0]
    total, done = len(ys) * len(xs), 0
    status("Upscaling…")
    for ty in ys:
        for tx in xs:
            if cancel.is_set():
                return
            y0, x0 = min(ty, max(0, h - tile)), min(tx, max(0, w - tile))
            piece = x[:, :, y0:y0 + tile, x0:x0 + tile].to("cuda", dtype)
            with torch.inference_mode():
                r = model(piece)[0].float().clamp(0, 1).cpu()
            out[:, y0 * scale:y0 * scale + r.shape[1], x0 * scale:x0 * scale + r.shape[2]] += r
            weight[:, y0 * scale:y0 * scale + r.shape[1], x0 * scale:x0 * scale + r.shape[2]] += 1
            done += 1
            emit({"t": "progress", "v": round(done / total, 3)})
    res = (out / weight.clamp(min=1)).permute(1, 2, 0).numpy()
    del out, weight
    final = Image.fromarray((res * 255).round().astype(np.uint8))
    buf = io.BytesIO()
    final.save(buf, format="JPEG", quality=94)
    emit({"t": "image", "v": base64.b64encode(buf.getvalue()).decode(), "width": final.width, "height": final.height})


def _gated(e: Exception) -> bool:
    text = f"{e.__class__.__name__} {e}"
    return any(w in text for w in ("GatedRepo", "401", "403", "gated", "RepositoryNotFound"))


def _takes_list(pipe) -> bool:
    # FLUX.2 pipelines take a list of reference images.
    return pipe.__class__.__name__.startswith("Flux2")


def job_search(p: dict, emit, cancel):
    """Web search through SearXNG. Nothing is kept; results go straight back to the phone."""
    import urllib.parse
    import urllib.request
    q = str(p.get("query", "")).strip()
    if not q:
        raise UserFacing("Nothing to search for.")
    url = SEARCH_URL.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": q, "format": "json"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "FixerDesktop/1"}),
                                    timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        raise UserFacing("Web search isn't available right now.") from e
    n = max(1, min(20, int(p.get("count", 6))))
    emit({"t": "info", "results": [{"title": x.get("title", ""), "url": x.get("url", ""), "content": x.get("content", "")}
                                   for x in data.get("results", [])[:n]]})


# ---------------------------------------------------------------- ComfyUI engine (trial, 11-16)
# ComfyUI v0.45.5 (pinned, own venv in C:\FixerDesktop\comfyui) runs headless on 127.0.0.1 only, started and killed
# with this server. Pictures come back over its websocket (SaveImageWebsocket): nothing is saved; its temp / input /
# output / user folders sit in COMFY_PRIVATE, wiped at every start and by "clear everything". Trial scope: new
# pictures with the recipes below; edits, video, cut-out and upscale stay on the diffusers path.

COMFY_DIR = ROOT / "comfyui"
COMFY_PRIVATE = ROOT / "comfy-private"
COMFY_PORT = int(os.environ.get("FIXER_COMFY_PORT", "8188"))
COMFY_ON = os.environ.get("FIXER_COMFY", "1") == "1"       # 0 = old diffusers path for everything
# 11-18: ComfyUI is started through this (beside this file): it refuses any request whose Host is not 127.0.0.1,
# localhost or [::1] with ComfyUI's port, so a web page using DNS rebinding cannot read or drive it. Knob comfy_guard.
COMFY_GUARD = Path(__file__).resolve().parent / "comfy_guard.py"
COMFY_GUARD_PROBE_HOST = "fixer-guard-check.invalid"


def _comfy_status(port: int, host: str | None, path: str = "/system_stats", timeout: float = 5) -> int:
    """One raw GET to ComfyUI with the Host header exactly as given (None = http.client's own, 127.0.0.1:port).
    Returns the HTTP status; 0 when there was no answer."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": host} if host is not None else {})
        r = conn.getresponse()
        r.read()
        return r.status
    except (OSError, http.client.HTTPException):
        return 0
    finally:
        conn.close()


def _comfy_guard_proof(port: int):
    """11-18: True when a forged Host is refused (403) and a normal request answered (200); otherwise a short reason
    (no request content) for the log line."""
    forged = _comfy_status(port, f"{COMFY_GUARD_PROBE_HOST}:{port}")
    normal = _comfy_status(port, None)
    if forged == 403 and normal == 200:
        return True
    return f"forged Host answered {forged}, normal {normal}"
COMFY_RECIPES = {                                               # phone model id -> Create recipe
    "Kandoo/Juggernaut-XL-Ragnarok": "juggernaut",
    "lodestones/Chroma1-HD": "chroma",
    "Comfy-Org/Qwen-Image-2.1": "qwen21",              # 11-17: Qwen-Image 2.1 Turbo (opt-in, makes and edits)
}
RETIRED_PICTURE_IDS = {   # old phone ids the app may still send -> the model that replaced them (NOT in COMFY_RECIPES)
    "Tongyi-MAI/Z-Image-Turbo": "Kandoo/Juggernaut-XL-Ragnarok",
    "tewea/z_image_turbo_bf16_nsfw": "Kandoo/Juggernaut-XL-Ragnarok",
}


def _snapshot(repo: str) -> Path | None:
    root = MODELS / "hub" / f"models--{repo.replace('/', '--')}"
    snaps = root / "snapshots"
    if not snaps.is_dir():
        return None
    try:                                                 # the revision the cache itself calls current (refs/main)
        cur = snaps / (root / "refs" / "main").read_text(encoding="utf-8").strip()
        if cur.parent == snaps and cur.is_dir():
            return cur
    except OSError:
        pass
    found = [d for d in snaps.glob("*") if d.is_dir()]
    return max(found, key=lambda d: (d.stat().st_mtime, d.name)) if found else None   # else the newest one


def _comfy_paths_yaml() -> str:
    """Points ComfyUI at the files already in Fixer's capped model folder (no second copy)."""
    ch = _snapshot("Comfy-Org/Chroma1-HD_repackaged")
    t5 = _snapshot("comfyanonymous/flux_text_encoders")
    up = _snapshot("Comfy-Org/Real-ESRGAN_repackaged")
    q21, q21te = _snapshot(QWEN21_REPO), _snapshot(QWEN21_TE_REPO)
    q21te_bf = _snapshot(QWEN21_TE_FALLBACK[0])
    q21x = _snapshot(_tune_text("qwen21_explicit_repo", QWEN21_LORA_REPO))
    wan = _snapshot(WAN14_REPO)
    wan_te = _snapshot(WAN_TE_REPO)
    wan_x = _snapshot(WAN_EXPLICIT_REPO)
    qe, qb, qa = _snapshot(QWEN_EDIT_REPO), _snapshot(QWEN_BASE_REPO), _snapshot(QWEN_AIO_REPO)
    jug = _snapshot("Kandoo/Juggernaut-XL-Ragnarok")
    song = _snapshot(SONG_REPO)
    def block(paths):
        return "|\n" + "".join(f"    {Path(x).as_posix()}\n" for x in paths if x)
    _ensure_lora_store()
    COMFY_VAE.mkdir(parents=True, exist_ok=True)
    return ("fixer:\n"
            f"  diffusion_models: {block([ch / 'split_files' / 'diffusion_models' if ch else None, wan / 'split_files' / 'diffusion_models' if wan else None, qe, q21 / 'diffusion_models' if q21 else None])}"
            f"  text_encoders: {block([t5, wan_te / 'split_files' / 'text_encoders' if wan_te else None, qb / 'split_files' / 'text_encoders' if qb else None, q21te, q21te_bf])}"
            f"  upscale_models: {block([up])}"
            f"  vae: {block([COMFY_VAE, wan / 'split_files' / 'vae' if wan else None, qb / 'split_files' / 'vae' if qb else None, q21 / 'vae' if q21 else None])}"
            f"  checkpoints: {block([qa / QWEN_AIO_DIR if qa else None, jug, song / 'checkpoints' if song else None])}"
            f"  loras: {block([COMFY_LORAS, wan / 'split_files' / 'loras' if wan else None, wan_x, q21x])}")


# ---- Real timings, from real use (11-17). Memory only (never disk), no prompt text, the last 100 finished
# picture / edit / video jobs. No test jobs: whatever the owner actually makes is what gets measured.
TIMINGS: collections.deque = collections.deque(maxlen=100)
_TL = threading.local()                                  # the record of the job running on this thread
_LOADER_CLASSES = frozenset({"UNETLoader", "CLIPLoader", "VAELoader", "CheckpointLoaderSimple", "LoraLoaderModelOnly",
                             "LoraLoader", "UpscaleModelLoader"})
_SAMPLER_CLASSES = frozenset({"SamplerCustomAdvanced", "KSampler", "KSamplerAdvanced"})


def _new_timing_record(kind: str) -> dict:
    return {"kind": kind, "model": None, "recipe": None, "editor": None, "explicit": False, "mp": None, "steps": None,
            "count": 1, "load_s": 0.0, "draw_s": 0.0, "engine_start_s": 0.0, "loaded": False, "runs": 0,
            "enhance": "off", "enhance_s": 0.0}


def _timing(**kw) -> None:
    """Notes what the running job is (called where its graph is chosen). A no-op outside a job."""
    r = getattr(_TL, "rec", None)
    if r is not None:
        r.update({k: v for k, v in kw.items() if v is not None})


class _TimingWatch:
    """Splits one ComfyUI run into "loading" and "drawing" from its own websocket events (nothing about the prompt).
    execution_start .. first sampler progress = loading (reading files, the text reader, weights onto the card);
    first sampler progress .. execution_success = drawing (steps, VAE decode, the PNG). `loaded` is True when ComfyUI
    had to (re)load a model: one of the workflow's loader nodes was not in execution_cached."""

    def __init__(self, workflow: dict, now=time.monotonic):
        self.now = now
        self.loaders = {str(k) for k, n in workflow.items() if (n or {}).get("class_type") in _LOADER_CLASSES}
        self.samplers = {str(k) for k, n in workflow.items() if (n or {}).get("class_type") in _SAMPLER_CLASSES}
        self.cached: set[str] = set()
        self.t_post = now()
        self.t_start = self.t_draw = self.t_end = None

    def on(self, mtype: str, d: dict) -> None:
        t = self.now()
        if mtype == "execution_start" and self.t_start is None:
            self.t_start = t
        elif mtype == "execution_cached":
            self.cached |= {str(x) for x in (d.get("nodes") or [])}
        elif mtype == "progress" and self.t_draw is None and str(d.get("node")) in self.samplers:
            self.t_draw = t
        elif mtype == "execution_success" or (mtype == "executing" and d.get("node") is None):
            self.t_end = t

    def result(self) -> dict:
        start = self.t_start if self.t_start is not None else self.t_post
        end = self.t_end if self.t_end is not None else self.now()
        draw = self.t_draw if self.t_draw is not None else start      # no sampler progress: all of it is "drawing"
        return {"load_s": max(0.0, draw - start), "draw_s": max(0.0, end - draw),
                "loaded": any(i not in self.cached for i in self.loaders)}

    def apply(self) -> None:
        r = getattr(_TL, "rec", None)
        if r is None:
            return
        x = self.result()
        r["load_s"] += x["load_s"]
        r["draw_s"] += x["draw_s"]
        r["loaded"] = bool(r["loaded"] or x["loaded"])
        r["runs"] += 1


def _timing_bucket(kind: str) -> str:
    return {"picture": "new picture", "pictures": "new picture", "edit": "edit", "change": "change"}.get(kind, kind)


def _timing_recent(n: int = 20) -> list[dict]:
    now = time.monotonic()
    out = []
    for r in list(TIMINGS)[::-1][:n]:
        d = {k: v for k, v in r.items() if k != "at"}
        for k in ("load_s", "draw_s", "total_s", "engine_start_s", "enhance_s", "mp"):
            if isinstance(d.get(k), float):
                d[k] = round(d[k], 2)
        d["age_s"] = int(now - r.get("at", now))
        out.append(d)
    return out


def _timing_summary() -> list[dict]:
    """Per (model, kind of job, add-on on/off): how many, average seconds PER PICTURE (a job of 4 pictures counts as
    its total divided by 4, so it compares with a 1-picture job), and loaded vs had-to-load averages. A paint "change"
    (a small crop) is its own kind, apart from full-photo edits. Failed jobs are left out. Most recently used first."""
    groups: dict[tuple, list[dict]] = {}
    for r in list(TIMINGS):
        if not r.get("ok") or r.get("total_s") is None:
            continue
        groups.setdefault((r.get("model") or r.get("kind"), _timing_bucket(r.get("kind", "")),
                           bool(r.get("explicit"))), []).append(r)

    def mean(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    def per(x, key):
        return x[key] / max(1, x.get("count") or 1)

    out = []
    for (model, bucket, explicit), rs in groups.items():
        warm = [per(x, "total_s") for x in rs if not x.get("loaded")]
        cold = [per(x, "total_s") for x in rs if x.get("loaded")]
        rate = [x["draw_s"] / (x["mp"] * x["steps"] * max(1, x.get("count") or 1)) for x in rs
                if x.get("mp") and x.get("steps") and x.get("draw_s")]
        out.append({"model": model, "kind": bucket, "explicit": explicit, "n": len(rs),
                    "total_s": mean([per(x, "total_s") for x in rs]), "draw_s": mean([per(x, "draw_s") for x in rs]),
                    "load_s": mean([per(x, "load_s") for x in rs]), "warm_total_s": mean(warm), "cold_total_s": mean(cold),
                    "s_per_mp_step": mean(rate), "_last": max(x.get("at", 0) for x in rs)})
    out.sort(key=lambda g: g["_last"], reverse=True)
    for g in out:
        del g["_last"]
    return out


def _secs(x) -> str:
    return f"{x:.0f} s" if x is not None and x >= 10 else f"{x:.1f} s" if x is not None else "?"


def _measured_speed_facts() -> list[str]:
    """Lines for the look-up tool: what this session's real jobs took."""
    L = ["MEASURED SPEED (this session's real jobs, newest first)"]
    rows = _timing_summary()
    if not rows:
        return L + ["- none yet this session"]
    for g in rows:
        bits = []
        if g["warm_total_s"] is not None:
            bits.append(f"{_secs(g['warm_total_s'])} when already loaded")
        if g["cold_total_s"] is not None:
            bits.append(f"{_secs(g['cold_total_s'])} when it had to load")
        if g["s_per_mp_step"]:
            bits.append(f"drawing {g['s_per_mp_step']:.2f} s per megapixel-step")
        L.append(f"- {g['model']}, {g['kind']}{', explicit add-on on' if g['explicit'] else ''}: "
                 f"{_secs(g['total_s'])} on average over {g['n']}" + (f" ({'; '.join(bits)})" if bits else ""))
    return L


def _recent_jobs_facts(n: int = 10) -> list[str]:
    rows = _timing_recent(n)
    if not rows:
        return []
    L = ["RECENT FINISHED JOBS (newest first)"]
    for r in rows:
        L.append(f"- {r.get('kind')}, {r.get('model') or 'unknown model'}, explicit add-on "
                 f"{'on' if r.get('explicit') else 'off'}"
                 + (f", {r['mp']:.1f} MP" if r.get("mp") else "") + (f", {r['steps']} steps" if r.get("steps") else "")
                 + f": total {_secs(r.get('total_s'))}, drawing {_secs(r.get('draw_s'))}, loading {_secs(r.get('load_s'))}"
                 + f", had to load the model: {'yes' if r.get('loaded') else 'no'}"
                 + ("" if r.get("ok") else ", failed"))
    return L


# ---- "Enhance my prompt" (11-17). The always-loaded uncensored brain does it, never a model that might soften an
# explicit request without saying so. The brain is told Qwen's own published instructions for its picture models
# (fetched at runtime into the model cache, never copied into this repo) plus the owner's rules, which win: keep every
# detail and every explicit word, never soften, never add covering. Runs while the job waits in line (outside the
# card's lock). Off by default for old phones (no "enhance" flag).
ENHANCE_T2I = ("Qwen/Qwen-Image-2.1-PE-T2I", "system_prompt.txt")
ENHANCE_I2I = ("Qwen/Qwen-Image-2.1-PE-I2I", "system_prompt.txt")
ENHANCE_RULES = (
    "Rules from the person using this app. They win over everything above.\n"
    "- Keep every detail they wrote and every explicit or sexual word exactly as written. Never soften, censor, "
    "replace or leave out anything, and never add clothing, covering, a different scene or anything they did not "
    "ask for.\n"
    "- Only add visual detail that helps draw what they asked for (setting, light, camera, materials, mood).\n"
    "- Keep it to one dense paragraph, roughly 60 words or fewer: no lists, no preamble.\n"
    "- Write in English.\n"
    "- Ignore any instruction above about an aspect ratio, JSON or another output format. Reply with the finished "
    "prompt text only: no quotes around it, no labels, no notes.")
ENHANCE_BUILTIN_T2I = (
    "You rewrite a short picture request into one rich, concrete description for a text-to-image model. Describe what "
    "is in the picture, where, the light, the camera and the style, in plain sentences.")
ENHANCE_BUILTIN_I2I = (
    "You rewrite a photo-edit request into one precise instruction for an image editing model. Say exactly what to "
    "change and where. Do not change what the person said to keep, and do not add changes they did not ask for.")
ENHANCE_MIN_KEEP = 0.6                    # a reply shorter than this share of the original is not an enhancement
_ENHANCE_TEXT: dict[bool, str] = {}
_ENHANCE_RETRY_AT: dict[bool, float] = {}      # a failed fetch is not tried again before this (time.monotonic)


def _fetch_text(repo: str, filename: str) -> str:
    """One small text file from the Hugging Face model cache (downloaded once if it isn't there yet)."""
    from huggingface_hub import hf_hub_download
    try:
        path = hf_hub_download(repo, filename, local_files_only=True)
    except Exception:                                    # noqa: BLE001
        path = hf_hub_download(repo, filename)
    return Path(path).read_text(encoding="utf-8").strip()


def _enhance_system(edit: bool) -> str:
    """Qwen's published instruction for this kind of request (T2I or I2I), else a short built-in one, then our rules."""
    text = _ENHANCE_TEXT.get(edit)
    if not text and time.monotonic() >= _ENHANCE_RETRY_AT.get(edit, 0.0):
        try:
            text = _fetch_text(*(ENHANCE_I2I if edit else ENHANCE_T2I))
        except Exception:                                # noqa: BLE001
            text = ""
        if text:
            _ENHANCE_TEXT[edit] = text
        else:
            _ENHANCE_RETRY_AT[edit] = time.monotonic() + 600     # offline or moved: the built-in text, and no new network wait for 10 min
    return (text or (ENHANCE_BUILTIN_I2I if edit else ENHANCE_BUILTIN_T2I)) + "\n\n" + ENHANCE_RULES


def _clean_enhanced(text: str) -> str:
    """The prompt from the brain's reply: no think block, no code fence, no JSON wrapper, no quotes around it."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t).strip()
    if t.startswith("{"):
        try:
            obj = json.loads(t)
            t = str(obj.get("rewritten_prompt") or obj.get("prompt") or "")
        except Exception:                                # noqa: BLE001
            m = re.search(r'"rewritten_prompt"\s*:\s*"((?:[^"\\]|\\.)*)"', t, re.S)
            if m:
                try:
                    t = json.loads('"' + m.group(1) + '"')
                except Exception:                        # noqa: BLE001
                    t = m.group(1)
            else:
                return ""                                # a cut-off wrapper: nothing usable, not half a JSON line
    t = t.strip()
    if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    return t[:4000]


def _enhance_port() -> int | None:
    """The running brain's port, starting it if it is off; None when it is paused or can't start."""
    if LLAMA.paused_for:
        return None
    if LLAMA.running() and LLAMA.port:
        return LLAMA.port
    choice = gguf_choice(LAST_BRAIN)
    if choice is None:
        return None
    return LLAMA.ensure(LAST_BRAIN, choice, lambda s: None)


def enhance_prompt(prompt: str, edit: bool, image_b64: str | None, cancel) -> tuple[str | None, str]:
    """(enhanced prompt or None, a plain-words note when it could not). Think off, text only back."""
    prompt = (prompt or "").strip()
    if not prompt:
        return None, ""
    try:
        port = _enhance_port()
    except Exception:                                    # noqa: BLE001
        port = None
    if port is None:
        return None, "The chat model isn't available right now, so your own words are used as they are."
    msg = {"role": "user", "text": prompt[:8000]}
    if edit and image_b64:
        msg["image"] = image_b64
    out: list[str] = []
    p = {"instructions": _enhance_system(edit), "messages": [msg], "think": False, "temperature": 0.5,
         "presence_penalty": 0.0, "max_tokens": int(_tune("enhance_max_tokens", 450)),   # 11-21: shorter = faster decode
         "_slot": PROMPT_SLOT}   # 11-19: never the chat's slot
    try:
        _chat_llama(p, lambda o: out.append(o.get("v", "")) if o.get("t") == "text" else None, cancel, port)
    except Exception:                                    # noqa: BLE001
        return None, "Improving the description didn't work, so your own words are used as they are."
    text = _clean_enhanced("".join(out))
    if not text or len(text) < ENHANCE_MIN_KEEP * len(prompt):
        return None, "Improving the description didn't work, so your own words are used as they are."
    return text, ""


# ----- Reference photo -> prompt (11-21): "Match this photo" --------------------------------------------------------
# The owner feeds a photo they like; the local VISION brain writes the prompt that would redraw it. Slice 1 returns
# the prompt, a negative and an 18+ flag only (a later slice suggests a model and add-ons through the role map). It is
# faithful, not inventive, and replies as JSON so it parses cleanly. Runs on the brain's prompt slot, like enhance.
DESCRIBE_SYSTEM = (
    "You look at ONE photo and write the prompt that would redraw it for a text-to-image model. Describe the subject, "
    "pose, body, clothing (or none), expression, setting, light, camera angle, style and colours in plain sentences. "
    "Describe exactly what is in the photo — never invent details that are not there. "
    "Reply with ONLY a JSON object and nothing else: "
    '{"prompt": "<the full description>", "negative": "<things to avoid, or empty>", "explicit": true or false}. '
    'Set "explicit" to true only when the photo shows nudity or sex.')


def _describe_system(safe: bool) -> str:
    base = DESCRIBE_SYSTEM
    if safe:
        base += ("\nThis is a safe session: describe people as clothed, keep the description non-sexual, and set "
                 '"explicit" to false.')
    extra = _tune_text("describe_system_extra", "")
    return base + ("\n" + extra if extra else "")


def _parse_describe(text: str) -> dict | None:
    """The brain's reply as {prompt, negative, explicit}, or None when there is no usable JSON object."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    t = re.sub(r"```[a-z]*", "", t)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:                                    # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    prompt = str(obj.get("prompt") or "").strip()
    if not prompt:
        return None
    flag = obj.get("explicit")
    if isinstance(flag, str) and flag.strip().lower() in ("", "false", "no", "off", "0", "none", "null"):
        flag = False                                     # a quoted "false" from the model is false, not a non-empty string
    return {"prompt": prompt[:4000], "negative": str(obj.get("negative") or "").strip()[:1000],
            "explicit": bool(flag)}


def describe_photo(image_b64: str, safe: bool, cancel) -> tuple[dict | None, str]:
    """(described dict or None, a plain-words note when it couldn't). Uses the brain's vision slot; think off."""
    try:
        port = _enhance_port()
    except Exception:                                    # noqa: BLE001
        port = None
    if port is None:
        return None, (PAUSED_TEXT if LLAMA.paused_for else
                      "The chat model isn't available right now, so a photo can't be matched.")
    if not LLAMA.vision:                                 # 11-21: fail loudly; a non-vision brain silently drops images
        return None, "This computer's chat model can't see images, so it can't match a photo."
    msg = {"role": "user", "text": "Describe this photo as an image prompt.", "image": image_b64}
    out: list[str] = []
    p = {"instructions": _describe_system(safe), "messages": [msg], "think": False,
         "temperature": _tune("describe_temp", 0.3), "presence_penalty": 0.0,
         "max_tokens": int(_tune("describe_max_tokens", 1100)),
         "_slot": PROMPT_SLOT}
    try:
        _chat_llama(p, lambda o: out.append(o.get("v", "")) if o.get("t") == "text" else None, cancel, port)
    except Exception:                                    # noqa: BLE001
        return None, "Matching the photo didn't work, so nothing was filled in."
    parsed = _parse_describe("".join(out))
    if parsed is None or len(parsed["prompt"]) < 20:
        return None, "The photo couldn't be described. Try another one."
    if safe:
        parsed["explicit"] = False                       # the 18+ switch, not the photo, decides in safe mode
    return parsed, ""


# ----- Macro review & recommendations (11-21) ----------------------------------------------------------------------
# The local brain reviews a MACRO (a reusable preset: prompt add, negative add, add-on roles/LoRAs, settings, model)
# and either suggests concrete improvements (a checklist the owner picks from) or proposes a few complete starter
# macros to adopt. Everything the brain returns is allow-listed against the real models/roles/library before it goes
# back, so a hallucinated model or add-on is dropped, and 18+ is forced off in a safe session.
def _brain_chat(system: str, user_text: str, cancel, temperature: float = 0.4, max_tokens: int = 1200) -> str | None:
    """A plain text-in / text-out brain call on the prompt slot (think off). None when the brain isn't available."""
    try:
        port = _enhance_port()
    except Exception:                                    # noqa: BLE001
        port = None
    if port is None:
        return None
    out: list[str] = []
    p = {"instructions": system, "messages": [{"role": "user", "text": user_text[:8000]}], "think": False,
         "temperature": temperature, "presence_penalty": 0.0, "max_tokens": int(max_tokens), "_slot": PROMPT_SLOT}
    try:
        _chat_llama(p, lambda o: out.append(o.get("v", "")) if o.get("t") == "text" else None, cancel, port)
    except Exception:                                    # noqa: BLE001
        return None
    return "".join(out)


def _brain_json(text: str):
    """The first JSON object in a brain reply (think block and fences stripped), or None."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    t = re.sub(r"```[a-z]*", "", t)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:                                    # noqa: BLE001
        return None


def _macro_text(m: dict) -> str:
    """A macro as readable lines for the brain."""
    parts = [f"Name: {m.get('name') or '(unnamed)'}"]
    if m.get("prompt_add"):
        parts.append(f"Prompt: {m['prompt_add']}")
    if m.get("negative_add"):
        parts.append(f"Negative: {m['negative_add']}")
    loras = [str((it.get('role') or it.get('file') or '?')) for it in (m.get('loras') or []) if isinstance(it, dict)]
    if loras:
        parts.append("Add-ons: " + ", ".join(loras))
    s = m.get("settings") if isinstance(m.get("settings"), dict) else {}
    if s:
        parts.append("Settings: " + ", ".join(f"{k}={v}" for k, v in s.items()))
    if m.get("model"):
        parts.append(f"Model: {m['model']}")
    parts.append(f"Explicit: {bool(m.get('explicit'))}")
    return "\n".join(parts)


def _lora_catalog_for_ai(safe: bool, limit: int = 64) -> list[str]:
    """The owner's INSTALLED LoRA add-ons as lines the brain can compose from: exact file id, family, name and a few
    trigger words (what each one is for). nsfw add-ons are dropped in a safe session. (11-21: the macro AI reads the
    real library so it recommends a combination of the owner's own add-ons, not generic role names.)"""
    out: list[str] = []
    for fn, e in (_lora_lib() or {}).items():
        if not isinstance(e, dict):
            continue
        if safe and _lora_is_explicit(fn, e):
            continue
        name = str(e.get("name") or re.sub(r"\.safetensors$", "", fn))[:70]
        fam = str(e.get("family") or "unknown")
        trig = [str(t) for t in (e.get("triggers") or []) if str(t).strip()][:3]
        st = e.get("strength")
        rs = f" · rec ~{float(st):.2g}" if isinstance(st, (int, float)) and 0 < float(st) <= 3 else ""
        tg = f" · triggers: {', '.join(trig)}" if trig else " · triggers: none known (do not invent)"
        out.append(f"- {fn}  [{fam}] {name}{rs}{tg}")
        if len(out) >= limit:
            break
    return out


def _macro_vocab(safe: bool) -> str:
    models = []
    for rid, r in CREATE_RECIPES.items():
        if not _recipe_ready(r) or (safe and rid in _adult_models()):
            continue
        fam = str(r.get("lora_family") or "")
        models.append(f"{rid} (family: {fam})" if fam else rid)
    lines = [f"Model ids you may use, each with the add-on FAMILY it takes: {', '.join(models) or 'none'}."]
    cat = _lora_catalog_for_ai(safe, int(_tune("macro_lora_cap", 64))) if int(_tune("macro_lora_catalog", 1)) else []
    if cat:
        cap = int(_tune("lora_cap", 4))
        lines.append(f"The owner's INSTALLED add-ons (LoRAs). Reference each by its exact file id; '[family]' must "
                     f"match the chosen model's family; 'rec ~N' is the creator's recommended strength when known. You "
                     f"may stack up to {cap}, but 2 to 4 is usually best:")
        lines.extend(cat)
    else:
        roles = list((_macros().get("roles") or {}).keys())
        lines.append(f"Add-on role names you may use for loras (or an empty list): {', '.join(roles) or 'none'}.")
    return "\n".join(lines)


def _recipe_family(mid) -> str:
    """The add-on family a model id takes ("" when the model or its family is unknown: nothing then filters)."""
    return str(CREATE_RECIPES.get(mid, {}).get("lora_family") or "") if mid else ""


def _lora_fits(it: dict, fam: str, lib: dict) -> bool:
    """A file add-on fits a model family when its own family matches, or is unknown (the computer couldn't sort it, so
    Make offers it everywhere). Role add-ons are the owner's own mapping and always fit."""
    f = it.get("file")
    if not f or not fam:
        return True
    return str((lib.get(f) or {}).get("family") or "unknown") in (fam, "unknown")


def _fit_loras(loras: list, fam: str) -> list:
    """The rules for an add-on stack, shared by a whole macro (_sanitize_macro) and an 'apply' patch (_clean_patch with
    context): keep the add-ons that fit the model's family, honor the dynamic add-on cap, then scale file-add-on
    strengths down proportionally when their sum is over the budget. Stacking LoRAs is not additive (weights compound),
    so a summed strength much over ~1.5 "fries" the image. (Research-backed heuristics.)"""
    if fam:
        lib = _lora_lib() or {}
        loras = [it for it in loras if _lora_fits(it, fam, lib)]
    cap = int(_tune("lora_cap", 4))
    if len(loras) > cap:
        loras = loras[:cap]
    files = [it for it in loras if it.get("file")]
    total = sum(float(it.get("strength", 1.0)) for it in files)
    budget = float(_tune("macro_strength_budget", 1.6))
    if 0 < budget < total:
        k = budget / total
        for it in files:
            it["strength"] = round(max(0.1, float(it.get("strength", 1.0)) * k), 2)
    return loras


def _clean_patch(ap: dict, safe: bool, ctx: dict | None = None) -> dict:
    """Keep only valid, provided macro fields from a brain-proposed change, coerced and allow-listed.
    `ctx` (11-22, finding 21): the macro being analysed (its "model" and "loras"). With it, the patch gets the same
    family / cap / budget trimming as a saved macro (_fit_loras), against the model the macro WOULD have after the
    patch; add-ons the trimming empties are left out (never written as []). A model switch that would leave the macro's
    own add-ons on the wrong family, with no add-on list of its own in the patch, is left out (the phone's merge keeps
    the old add-ons, so the switch would break them). Without ctx nothing of this applies."""
    out: dict = {}
    # 11-22: a key whose cleaned value is empty (null, invalid, every entry dropped) is left OUT, not written as ""/[]:
    # the phone's Macro.merge overwrites whatever key is present, so an empty one would blank the owner's own text.
    if isinstance(ap.get("prompt_add"), str) and ap["prompt_add"].strip():
        out["prompt_add"] = ap["prompt_add"][:2000]
    if isinstance(ap.get("negative_add"), str) and ap["negative_add"].strip():
        out["negative_add"] = ap["negative_add"][:1000]
    if "explicit" in ap:
        out["explicit"] = bool(ap["explicit"]) and not safe
    if isinstance(ap.get("settings"), dict):
        s = {}
        for k in ("cfg", "steps", "shift"):
            if k in ap["settings"]:
                try:
                    s[k] = float(ap["settings"][k])
                except (TypeError, ValueError):
                    pass
        if s:
            out["settings"] = s
    if "model" in ap:
        mid = str(ap["model"] or "")
        if mid in CREATE_RECIPES and _recipe_ready(CREATE_RECIPES[mid]) and not (safe and mid in _adult_models()):
            out["model"] = mid
    if isinstance(ap.get("loras"), list):
        role_map = _macros().get("roles") or {}
        roles = set(role_map.keys())
        lib = _lora_lib() or {}
        kept = []
        for it in ap["loras"]:
            if not isinstance(it, dict):
                continue
            if it.get("role") and str(it["role"]) in roles:
                rn = str(it["role"])
                if safe:                                 # 11-22: a safe session takes no explicit role (name or its file)
                    rf = str((role_map.get(rn) or {}).get("file") or "")
                    if rn in ("undress-helper", "qwen-explicit-addon") or (rf and _lora_is_explicit(rf, lib.get(rf))):
                        continue
                kept.append({"role": rn})
            elif it.get("file") and str(it["file"]) in lib:
                fn = str(it["file"])
                if safe and _lora_is_explicit(fn, lib.get(fn)):
                    continue                             # 11-22: the same 18+ test as the rest of the app
                try:
                    st = float(it.get("strength", 1.0))
                except (TypeError, ValueError):
                    st = 1.0                             # null or a range like "0.5-0.8": use the default strength
                kept.append({"file": fn, "strength": max(0.0, min(2.0, st))})
        if kept:
            out["loras"] = kept
    if isinstance(ctx, dict):
        mid = out.get("model") or str(ctx.get("model") or "")
        fam = _recipe_family(mid)
        if "loras" in out:
            fitted = _fit_loras(out["loras"], fam)
            if fitted:
                out["loras"] = fitted
            else:
                del out["loras"]                         # all of them were the wrong family: leave the add-ons alone
        elif out.get("model") and fam:
            lib = _lora_lib() or {}
            have = [it for it in (ctx.get("loras") or []) if isinstance(it, dict)]
            if not all(_lora_fits(it, fam, lib) for it in have):
                del out["model"]                         # the macro's own add-ons would no longer match this model
    return out


def _sanitize_macro(raw: dict, safe: bool) -> dict | None:
    """A whole brain-proposed macro, coerced to the real schema. None when there's nothing usable."""
    if not isinstance(raw, dict):
        return None
    import uuid
    patch = _clean_patch(raw, safe)
    # 11-22: enforce add-on family against the chosen model. The AI is TOLD to match family but nothing enforced it,
    # so a suggestion could pair an sdxl add-on with a chroma model — which then reached the phone and showed up as
    # "not available here" when used. Only a model with a known family filters; an "unknown"-family add-on still
    # passes (the computer couldn't sort it, so Make offers it everywhere). Role add-ons are the owner's own mapping.
    mid = patch.get("model")
    if isinstance(patch.get("loras"), list):
        patch["loras"] = _fit_loras(patch["loras"], _recipe_family(mid))
    prompt_add = patch.get("prompt_add") or str(raw.get("prompt") or "")[:2000]
    name = str(raw.get("name") or "Suggested macro")[:80]
    if not prompt_add and not patch.get("loras"):
        return None
    return {"id": str(raw.get("id") or uuid.uuid4().hex[:8]), "name": name, "prompt_add": prompt_add,
            "negative_add": patch.get("negative_add", ""), "loras": patch.get("loras", []),
            "settings": patch.get("settings", {}), "model": patch.get("model"),
            "explicit": patch.get("explicit", False)}


def _analyze_macro_system(safe: bool) -> str:
    base = (
        "You review ONE image-generation MACRO (a reusable preset: a prompt addition, a negative addition, add-on "
        "LoRAs, picture settings like cfg/steps, and maybe a model). Suggest concrete, specific improvements for "
        "sharper, more realistic, more on-target images. Look for redundant or conflicting add-ons, prompt phrases "
        "that repeat or fight, missing detail, a cfg/steps value that is off, or trigger words worth adding. Give a "
        "SHORT list (3 to 6) of discrete suggestions. "
        "IMPORTANT: each suggestion AMENDS the macro — it does not rewrite it. So in 'apply', 'prompt_add' and "
        "'negative_add' are a SHORT piece of text to ADD (e.g. trigger words or one phrase), never a rewritten prompt, "
        "and 'loras' are add-ons to ADD to the existing stack. Only 'settings', 'model' and 'explicit' are set "
        "outright (they are single values). Keep each addition small and specific. "
        'Reply with ONLY JSON: {"suggestions": [{"title": "<short>", "detail": "<one sentence why>", '
        '"apply": {<only the fields to add/set>}}]}. Include "apply" only when the change is concrete, e.g. '
        '{"negative_add": "blurry, extra fingers"} (text to add) or {"settings": {"cfg": 3.0}}. Never invent add-ons '
        "or models not already present.")
    return base + ("\nThis is a safe session: keep every suggestion non-explicit." if safe else "")


def _recommend_macros_system(safe: bool) -> str:
    base = (
        "You are a professional image-generation prompt engineer. You design reusable MACROS — each a COMPLETE recipe: "
        "a name, a prompt addition, a negative addition, add-on LoRAs with strengths, picture settings (cfg/steps), a "
        "model, and an explicit flag. A macro is a full starting point, so ALWAYS set a real model from the list below "
        "(never null). Compose like a pro:\n"
        "- Pick ONE model first, then only add-ons whose [family] matches it (a mismatched add-on does nothing).\n"
        "- One add-on per CONCEPT SLOT (style, subject/character, detail/lighting); never stack two of the same kind, "
        "they fight. 2 to 4 add-ons is the sweet spot; use more only when each has a clearly distinct role.\n"
        "- Strength: start from an add-on's 'rec ~N' when shown and treat it as the ceiling; otherwise style 0.5-0.8, "
        "character 0.7-1.0, detail 0.3-0.5. Keep the strengths' SUM near 1.0-1.5; when stacking 3 or more, scale each "
        "down to about 60-80%. A speed/distill add-on uses its exact recommended strength and settings.\n"
        "- Put each used add-on's trigger words VERBATIM into the prompt addition. If an add-on lists no triggers, do "
        "not invent any.\n"
        "- Prompt weighting: for an sdxl model you MAY emphasise sparingly with (phrase:1.1-1.4); for qwen or chroma "
        "models do NOT use weighting syntax (they print it literally) — use natural wording.\n"
        "- Prompt shape: sdxl takes tags plus short phrases; chroma and qwen take full natural-language sentences.\n"
        "- Settings: set cfg/steps only when the add-on recommends specific ones, else omit them to keep the model's "
        "defaults. Never pair a speed/distill add-on with high cfg or many steps.\n"
        "Propose 2 to 4 distinct, genuinely useful macros the owner can adopt and tweak. "
        "STRONGLY PREFER the models and add-ons the owner ALREADY HAS: build the macros from the INSTALLED models and "
        "the INSTALLED add-on list below, and only ever put installed add-ons in a macro's 'loras'. "
        "The 'wishlist' (0 to 3 items) is OPTIONAL and only for a real improvement that the installed set genuinely "
        "can't achieve — not a nicer-sounding duplicate of something installed. Favour wishlist add-ons that fit a "
        "model family the owner already has. You MAY suggest one that needs a DIFFERENT model than the owner has, but "
        "only when the improvement is substantial, and then 'why' must say plainly that it's an upgrade requiring that "
        "model. Each wishlist item: 'find' is a 2-4 word search phrase for a REAL add-on (it will be checked against "
        "the live catalog and dropped if it doesn't exist, so describe a real, common kind), 'family' is the model "
        "family it fits (from the list below, or \"\" if unsure), 'why' is one short sentence. Never put an "
        "uninstalled add-on in a macro.\n"
        'Reply with ONLY JSON: {"macros": [{"name": "...", "prompt_add": "...", "negative_add": "...", '
        '"loras": [{"file": "<exact installed file id>", "strength": 0.8}], "settings": {"cfg": 3.5, "steps": 30}, '
        '"model": "<model id>", "explicit": false}], '
        '"wishlist": [{"find": "film grain photo", "family": "sdxl", "why": "adds analog texture these lack"}]}.\n'
        + _macro_vocab(safe))
    return base + ("\nThis is a safe session: every macro must be non-explicit (set explicit false) and use no nsfw "
                   "add-ons." if safe else "")


def job_analyze_macro(p: dict, emit, cancel):
    """Review a macro -> a short list of pickable suggestions."""
    macro = p.get("macro")
    if not isinstance(macro, dict):
        raise UserFacing("No macro to review.")
    safe = p.get("safe") is True
    emit({"t": "status", "v": "Reviewing your macro…"})
    text = _brain_chat(_analyze_macro_system(safe), _macro_text(macro), cancel,
                       temperature=_tune("macro_temp", 0.4), max_tokens=int(_tune("macro_max_tokens", 1200)))
    if text is None:
        raise UserFacing(PAUSED_TEXT if LLAMA.paused_for else "The chat model isn't available right now.")
    obj = _brain_json(text)
    if not isinstance(obj, dict) or not isinstance(obj.get("suggestions"), list):
        # 11-22: a cut-off or garbled reply is a failure, not "it looks good" (an empty list means the model reviewed it)
        raise UserFacing("Couldn't review it. Try again.")
    import uuid
    out = []
    for s in (obj.get("suggestions") or [])[:8]:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        if not title:
            continue
        item = {"id": uuid.uuid4().hex[:8], "title": title[:120], "detail": str(s.get("detail") or "")[:300]}
        if isinstance(s.get("apply"), dict) and s["apply"]:
            try:
                patch = _clean_patch(s["apply"], safe, macro)
            except Exception:                            # noqa: BLE001  (one odd suggestion must not sink the rest)
                continue
            if not patch:
                continue                                 # its change was all invalid (an invented add-on...): drop it
            item["apply"] = patch                        # (advice only: no apply, or {}, is kept as plain advice)
        out.append(item)
    emit({"t": "info", "suggestions": out})


def job_recommend_macros(p: dict, emit, cancel):
    """Propose a few complete starter macros."""
    safe = p.get("safe") is True
    ctx = str(p.get("context") or "").strip()
    user = f"What I'm going for: {ctx}" if ctx else "Suggest some useful macros."
    emit({"t": "status", "v": "Thinking up some macros…"})
    text = _brain_chat(_recommend_macros_system(safe), user, cancel,
                       temperature=_tune("macro_temp", 0.6), max_tokens=int(_tune("macro_rec_max_tokens", 1600)))
    if text is None:
        raise UserFacing(PAUSED_TEXT if LLAMA.paused_for else "The chat model isn't available right now.")
    obj = _brain_json(text)
    if not isinstance(obj, dict) or not isinstance(obj.get("macros"), list):
        raise UserFacing("Couldn't come up with macros. Try again.")   # 11-22: not "no suggestions" when the reply was cut off
    macros = []
    for r in obj["macros"][:6]:
        try:
            m = _sanitize_macro(r, safe)
        except Exception:                                # noqa: BLE001  (one odd macro must not sink the others)
            m = None
        if m:
            macros.append(m)
    emit({"t": "info", "macros": macros, "wishlist": _clean_wishlist(obj.get("wishlist"), safe)})


def _catalog_lookup(query: str, family: str, safe: bool, limit: int = 2) -> list[dict]:
    """Search the REAL Civitai catalog for a concept and return a few ACTUAL add-ons (name, base model, resolved
    family, size, downloads, trigger words, source_id). Used to VERIFY an AI wishlist suggestion exists before it is
    ever shown, so the chat brain cannot invent add-ons that aren't real. `family` scopes the base models when known;
    if nothing is found in-family it retries broadly (still real hits). Returns [] on any error, so a down or blocked
    catalog drops the suggestion rather than faking one."""
    import urllib.parse
    q = (query or "").strip()
    if not q:
        return []
    try:
        bases = LORA_CIVITAI_BASE.get(family, []) if family else []
        base_params = [("types", "LORA"), ("types", "LoCon"), ("types", "DoRA"),
                       ("nsfw", "false" if safe else "true"), ("limit", "12"),
                       ("sort", "Most Downloaded"), ("query", q)]
        params = base_params + [("baseModels", b) for b in bases]
        data = _api_json(f"{CIVITAI_API}/models?" + urllib.parse.urlencode(params)) or {}
        items = data.get("items") or []
        if not items and bases:                          # nothing in that family: widen to any base (still real)
            data = _api_json(f"{CIVITAI_API}/models?" + urllib.parse.urlencode(base_params)) or {}
            items = data.get("items") or []
        out: list[dict] = []
        for m in items:
            if safe and m.get("nsfw"):
                continue
            v = (m.get("modelVersions") or [{}])[0]
            rows = _civitai_file_rows(v.get("files"))
            if not rows:
                continue
            base = v.get("baseModel", "")
            fam = _resolve_family(base or "", None)[0] or ""
            out.append({"name": str(m.get("name", ""))[:80], "base": base, "family": fam,
                        "source_id": f"civitai:{m.get('id')}@{v.get('id')}",
                        "downloads": (m.get("stats") or {}).get("downloadCount"),
                        "size_mb": round(sum(r["size"] for r in rows) / 1024 ** 2, 1),
                        "triggers": [str(t) for t in (v.get("trainedWords") or [])][:4]})
            if len(out) >= limit:
                break
        return out
    except Exception:                                    # noqa: BLE001
        return []


def _clean_wishlist(raw, safe: bool) -> list[dict]:
    """AI-proposed add-on CONCEPTS worth acquiring — each VERIFIED against the real catalog so nothing invented is
    shown. Every kept item carries its target family + model name, whether the owner already has a usable model for
    it (so the phone can say "you'd also need model X"), and the real add-ons found. At most 3."""
    if not isinstance(raw, list):
        return []
    known = set(LORA_FAMILIES)
    adult = _adult_models()
    have = {str(r.get("lora_family") or "") for rid, r in CREATE_RECIPES.items()
            if r.get("lora_family") and _recipe_ready(r) and not (safe and rid in adult)}
    out: list[dict] = []
    for it in raw[:6]:
        if not isinstance(it, dict):
            continue
        find = str(it.get("find") or "").strip()[:60]
        if not find:
            continue
        fam = str(it.get("family") or "").strip()
        fam = fam if fam in known else ""
        hits = _catalog_lookup(find, fam, safe, limit=2)
        if not hits:                                     # no real add-on exists for this -> drop it (no hallucinations)
            continue
        target = hits[0].get("family") or fam            # trust the real result's family over the AI's guess
        out.append({"find": find, "family": target, "why": str(it.get("why") or "").strip()[:160],
                    "model_label": _family_model_name(target) if target else "",
                    "have_model": target in have, "results": hits})
    # Prefer add-ons that work with a model the owner already has; upgrades (new model needed) come last. Keep 3.
    out.sort(key=lambda w: 0 if w["have_model"] else 1)
    return out[:3]


def _enhance_inputs(op: str, payload: dict | None):
    """(prompt, is_edit, photo) when this request asks to be enhanced, else None. Paint (a spot's own words) is never."""
    if not isinstance(payload, dict):
        return None
    if op == "image":
        if payload.get("enhance") is True and payload.get("prompt"):
            return payload["prompt"], bool(payload.get("image")), payload.get("image")
        return None
    if op == "comfy_run":
        v = payload.get("values") or {}
        if v.get("enhance") is True and v.get("prompt") and v.get("image_mode") != "paint":
            return v["prompt"], bool(v.get("image") and v.get("image_mode") in ("edit", "reference")), v.get("image")
    return None


def _with_prompt(op: str, payload: dict, text: str) -> dict:
    if op == "comfy_run":
        return {**payload, "values": {**(payload.get("values") or {}), "prompt": text}}
    return {**payload, "prompt": text}


def _enhance_job(job) -> None:
    """Runs in a thread right after the job is lined up: the job waits for it (if it reaches the front first)."""
    t0 = time.monotonic()
    try:
        got = _enhance_inputs(job.op, job.payload)
        if got is None or job.cancel.is_set():
            return
        text, note = enhance_prompt(got[0], got[1], got[2], job.cancel)
        if job.cancel.is_set():
            return
        job.enhanced, job.enhance_note = text, note
        job.enhance_state = "on" if text else "failed"
        if text:
            job.emit({"t": "enhanced", "v": text})
        elif note:
            job.emit({"t": "info", "enhance_skipped": note})
    except Exception:                                    # noqa: BLE001
        job.enhance_state = "failed"
        job.enhance_note = "Improving the description didn't work, so your own words are used as they are."
    finally:
        job.enhance_s = round(time.monotonic() - t0, 2)
        job.enhance_done.set()


def _prepare_pictures(target, p: dict | None, emit, cancel) -> None:
    """11-19: a picture job whose words are still being improved used to wait for them BEFORE starting the picture
    engine and loading the recipe's models, so the two ran one after the other. Now, while the brain writes, the
    engine starts and (when the recipe is known and isn't already loaded) a 1-step warm-up reads its models in.
    Only beside the MoE brain: it shares the card with ComfyUI, while a dense brain would be stopped for pictures
    mid-write (_room_for_pictures), and never when a picture RAM budget is set (that check may pause the brain).
    Best effort: anything that fails here simply happens again in the job itself."""
    if not isinstance(p, dict) or not _is_moe(LAST_BRAIN) or _tune("picture_ram_gb", 0) > 0:
        return
    rec, _TL.rec = getattr(_TL, "rec", None), None     # the warm-up must not count in this job's timings
    try:
        COMFY.ensure_current(lambda s: emit({"t": "status", "v": s}))
        if target is job_comfy_image:
            r = _picture_recipe(p)
        elif target is job_comfy_run and (p.get("values") or {}).get("image_mode") not in ("edit", "reference"):
            r = CREATE_RECIPES.get(p.get("recipe") or "")
        else:
            r = None                                   # edits run the editor's models: the engine start is the win
        rid = _recipe_id(r) if r else None
        if (not rid or rid == COMFY.last_recipe or (getattr(_TL, "safe", False) and rid in _adult_models())
                or not _recipe_ready(CREATE_RECIPES[rid]) or cancel.is_set()):
            return
        asyncio.run(_comfy_run(_warmup_workflow(rid), lambda obj: None, cancel))
        if not cancel.is_set():
            COMFY.last_recipe = rid
    except Exception as e:                             # noqa: BLE001
        log.info("picture prep skipped: %s", e.__class__.__name__)
    finally:
        _TL.rec = rec


def _apply_enhance(job, payload, emit):
    """At the job's start: wait for its enhancement (usually done while it queued) and draw from the enhanced words;
    if there are none, say why in plain words and draw from the original ones."""
    if not getattr(job, "enhance", False) or payload is None:
        return payload
    if not job.enhance_done.is_set():
        emit({"t": "status", "v": "Improving your description…"})
        limit = time.monotonic() + _tune("enhance_wait_s", 240)
        while not job.enhance_done.wait(0.5):
            if job.cancel.is_set() or time.monotonic() > limit:
                return payload
    if job.enhanced:
        return _with_prompt(job.op, payload, job.enhanced)
    if job.enhance_note:
        emit({"t": "status", "v": job.enhance_note})
    return payload


class Comfy:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.warmed: set[str] = set()
        self._last_recipe: str | None = None  # 11-19: the recipe whose models ComfyUI last ran (so is holding now)
        self.last_picture: str | None = None  # 11-19: the last recipe it ran at all (kept by a free / stop)
        self.paths = ""
        self.reserve = 0.0                   # --reserve-vram GB this start used (0 = none)
        self.guarded: bool | None = None     # 11-18: this start went through the proven host guard

    def available(self) -> bool:
        return COMFY_ON and (COMFY_DIR / "main.py").is_file() and (COMFY_DIR / ".venv").is_dir()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _get(self, path: str, timeout: float = 3):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{COMFY_PORT}{path}", timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def _post(self, path: str, body: dict, timeout: float = 30):
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{COMFY_PORT}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return json.loads(data) if data else None

    def ensure(self, status=lambda s: None):
        import shutil
        with self.lock:
            if self.running():
                return
            shutil.rmtree(COMFY_PRIVATE, ignore_errors=True)
            for d in ("temp", "input", "output", "user"):
                (COMFY_PRIVATE / d).mkdir(parents=True, exist_ok=True)
            self.paths = _comfy_paths_yaml()
            (COMFY_PRIVATE / "paths.yaml").write_text(self.paths, encoding="utf-8")
            status("Starting the picture engine…")
            guarded = _tune("comfy_guard", 1) >= 1     # 11-18: 0 = the old start (python -s main.py), unguarded
            if guarded and not COMFY_GUARD.is_file():
                log.warning("ComfyUI host guard missing; not starting ComfyUI")
                raise UserFacing("The picture engine on your computer couldn't start.")
            script = str(COMFY_GUARD) if guarded else "main.py"
            args = [str(COMFY_DIR / ".venv" / "Scripts" / "python.exe"), "-s", script,
                    "--listen", "127.0.0.1", "--port", str(COMFY_PORT), "--disable-auto-launch",
                    "--temp-directory", str(COMFY_PRIVATE / "temp"), "--input-directory", str(COMFY_PRIVATE / "input"),
                    "--output-directory", str(COMFY_PRIVATE / "output"), "--user-directory", str(COMFY_PRIVATE / "user"),
                    "--extra-model-paths-config", str(COMFY_PRIVATE / "paths.yaml"),
                    "--disable-metadata", "--preview-method", "auto",   # live previews over the websocket
                    "--offline"]                       # 11-17 audit: no partner nodes, nothing reaches the internet
            reserve = _tune("comfy_reserve_gb", 6.0)
            if reserve > 0 and _is_moe(LAST_BRAIN):
                # 11-17: the MoE brain's share of the card (attention, shared weights, cache, photo reader ~4-6 GB)
                # stays free, so ComfyUI never plans to use it and the brain can (re)start beside a running picture.
                args += ["--reserve-vram", f"{reserve:.1f}"]
            self.reserve = reserve if "--reserve-vram" in args else 0.0
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            # 11-17 (fast switches): ComfyUI's default RAM-pressure cache drops every model of an earlier workflow as
            # soon as free RAM is under its "inactive" headroom, which defaults to ALL of the RAM (execution.py:809),
            # so every switch between picture models re-read the files. Inactive headroom 6 GB keeps earlier
            # models cached until free RAM really runs short; active headroom 3 GB (default would be 6.4) lets the
            # current job keep its big intermediates. Budget on 64 GB: MoE brain ~20 GB, Chroma 14.7 GB (fp8 model 9.2
            # + T5 fp8 5.2 + ae 0.3), Qwen 2.1 14.3 GB (int8 7.3 + heretic W4A8 reader 6.3 + VAE 0.7), Windows and
            # apps ~8-10 GB: both picture models stay, ~6-8 GB free. A third picture model pushes out the oldest
            # by ComfyUI's own score. Video still frees everything first (_room_for_video). Not --high-ram (no limit
            # at 64 GB: Windows would page) and not --cache-classic. Knobs (apply on ComfyUI's next start):
            # comfy_cache_active_gb / comfy_cache_inactive_gb in local.json.
            args += ["--cache-ram", f"{_tune('comfy_cache_active_gb', 3.0):g}",
                     f"{_tune('comfy_cache_inactive_gb', 6.0):g}"]
            t_spawn = time.monotonic()
            self.proc = subprocess.Popen(args, cwd=str(COMFY_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, creationflags=flags)
            _JOB.add(self.proc)                        # dies with this server, however it exits
            deadline = time.time() + 180
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    self.proc = None
                    raise UserFacing("The picture engine on your computer couldn't start.")
                try:
                    self._get("/system_stats")
                except Exception:
                    time.sleep(0.5)
                    continue
                if guarded:
                    # 11-18: prove the guard works before anything is sent: a forged Host must be refused (403) and a
                    # normal request answered (200). If not, stop ComfyUI and refuse to use it (fail closed).
                    proof = _comfy_guard_proof(COMFY_PORT)
                    if proof is not True:
                        log.warning("ComfyUI host guard check failed (%s); ComfyUI stopped", proof)
                        self._kill_locked()
                        raise UserFacing("The picture engine on your computer couldn't start.")
                self.guarded = guarded
                _timing(engine_start_s=round(time.monotonic() - t_spawn, 1))
                return
            log.warning("ComfyUI did not answer within 180 s; stopped so a later job starts it fresh and proven")
            self._kill_locked()                        # never leave an unproven half-started process for ensure() to trust
            raise UserFacing("The picture engine on your computer is taking too long to start.")

    def ensure_current(self, status=lambda s: None):
        """Like ensure(), but restarts first if model folders changed since start (a fresh download)."""
        if self.running() and self.paths != _comfy_paths_yaml():
            self.stop()
        self.ensure(status)

    def free(self):
        """Moves ComfyUI's models off the graphics card (the chat model or the diffusers helper needs it)."""
        if self.running():
            self.last_recipe = None
            try:
                self._post("/free", {"unload_models": True, "free_memory": True})
            except Exception:
                pass

    @property
    def last_recipe(self) -> str | None:
        return self._last_recipe

    @last_recipe.setter
    def last_recipe(self, rid: str | None):
        self._last_recipe = rid
        if rid:
            self.last_picture = rid                    # what to bring back after a video (_after_job)

    def unload_card(self):
        """11-19: moves ComfyUI's models off the graphics card but keeps them in RAM (no free_memory: that also
        resets the node cache, so the next picture would read the disk again)."""
        if self.running():
            try:
                self._post("/free", {"unload_models": True, "free_memory": False})
            except Exception:
                pass

    def guard_state(self) -> bool | None:
        """11-18: for /local/state: True = started through the proven host guard, False = started without it
        (comfy_guard 0), None = not running."""
        return self.guarded if self.running() else None

    def _kill_locked(self):
        """Kills ComfyUI and wipes its private folders; the caller holds self.lock."""
        import shutil
        if self.running():
            self.proc.kill()
            try:
                self.proc.wait(10)
            except Exception:
                pass
        self.proc = None
        self.guarded = None
        self.warmed.clear()
        self.last_recipe = None
        shutil.rmtree(COMFY_PRIVATE, ignore_errors=True)

    def stop(self):
        with self.lock:
            self._kill_locked()


COMFY = Comfy()


# Photo edits (11-17): Qwen-Image-Edit-2511 replaces FLUX.2 klein, which ignored pose changes ("make them sit") and
# toned explicit edits down. Up to three photos ("the shirt from image 2"). Stock 2511 with the 4-step Lightning
# merge for ordinary edits; Phr00t's Rapid-AIO NSFW merge (one checkpoint: model, text encoder and VAE) for explicit.
QWEN_EDIT_REPO = "lightx2v/Qwen-Image-Edit-2511-Lightning"
QWEN_EDIT_UNET = "qwen_image_edit_2511_fp8_e4m3fn_scaled_lightning_comfyui_4steps_v1.0.safetensors"
QWEN_BASE_REPO = "Comfy-Org/Qwen-Image"          # its text encoder (Qwen2.5-VL 7B fp8) and VAE
QWEN_TE = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
QWEN_VAE = "qwen_image_vae.safetensors"
QWEN_AIO_REPO = "Phr00t/Qwen-Image-Edit-Rapid-AIO"
QWEN_AIO_DIR = "v19"
QWEN_AIO_FILE = "Qwen-Rapid-AIO-NSFW-v19.safetensors"
QWEN_EXPLICIT_RECIPES: set = set()        # no Make model is explicit-by-default now (explicit comes from the words)


# Off until it is measured on this PC (11-17: switched on untested, its first load clogged the card and every
# request waited). Turn on with "qwen_edit": true in local.json or FIXER_QWEN_EDIT=1.
QWEN_EDIT_ON = os.environ.get("FIXER_QWEN_EDIT") == "1" or bool(LOCAL.get("qwen_edit"))


def _qwen_edit_ready(explicit: bool = False) -> bool:
    if not QWEN_EDIT_ON:
        return False
    if explicit:
        a = _snapshot(QWEN_AIO_REPO)
        return bool(a and (a / QWEN_AIO_DIR / QWEN_AIO_FILE).is_file())
    e, b = _snapshot(QWEN_EDIT_REPO), _snapshot(QWEN_BASE_REPO)
    return bool(e and b and (e / QWEN_EDIT_UNET).is_file()
                and (b / "split_files" / "text_encoders" / QWEN_TE).is_file()
                and (b / "split_files" / "vae" / QWEN_VAE).is_file())


def _qwen_explicit(prompt: str, recipe_id: str | None = None) -> bool:
    """Explicit words, or an explicit Make model, and the explicit editor is here."""
    return (_is_explicit(prompt) or (recipe_id or "") in QWEN_EXPLICIT_RECIPES) and _qwen_edit_ready(True)


def _qwen_edit_workflow(prompt: str, images: list, seed: int, w: int, h: int, explicit: bool,
                        steps: int = 4, loras: list | None = None) -> dict:
    """Qwen-Image-Edit-2511 as ComfyUI's own template builds it: each photo (image 1 is the base and sets the shape)
    goes to the vision model and as a reference latent; 4 steps, CFG 1. `loras` chain after the model output."""
    import re
    # The node labels the photos "Picture 1/2/3"; the phone says "photo 2", people say "image 3" or "pic 1".
    prompt = re.sub(r"\b(?:photo|image|pic|picture)\s*#?\s*([123])\b", r"Picture \1", prompt, flags=re.IGNORECASE)
    if explicit:
        wf = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": QWEN_AIO_FILE}}}
        model, clip, vae = ["1", 0], ["1", 1], ["1", 2]
        sampler, scheduler = "euler_ancestral", "beta"      # the merge's own recommendation
    else:
        wf = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": QWEN_EDIT_UNET, "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_TE, "type": "qwen_image", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}},
            "30": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.1}},
            "31": {"class_type": "CFGNorm", "inputs": {"model": ["30", 0], "strength": 1.0}},
        }
        model, clip, vae = ["31", 0], ["2", 0], ["3", 0]
        sampler, scheduler = "euler", "simple"
    for i, (name, strength) in enumerate(loras or []):            # LoRAs chain on the model, before the sampler
        nid = f"7{i}0"
        wf[nid] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": model, "lora_name": name, "strength_model": float(strength)}}
        model = [nid, 0]
    enc = {"clip": clip, "vae": vae}
    for i, name in enumerate(images[:3]):
        wf[f"1{i}0"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        enc[f"image{i + 1}"] = [f"1{i}0", 0]
    wf.update({
        "20": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {**enc, "prompt": prompt}},
        "21": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {**enc, "prompt": ""}},
        "22": {"class_type": "FluxKontextMultiReferenceLatentMethod",
               "inputs": {"conditioning": ["20", 0], "reference_latents_method": "index_timestep_zero"}},
        "23": {"class_type": "FluxKontextMultiReferenceLatentMethod",
               "inputs": {"conditioning": ["21", 0], "reference_latents_method": "index_timestep_zero"}},
        "40": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "50": {"class_type": "KSampler", "inputs": {"model": model, "seed": seed, "steps": int(steps), "cfg": 1.0,
                                                    "sampler_name": sampler, "scheduler": scheduler,
                                                    "positive": ["22", 0], "negative": ["23", 0],
                                                    "latent_image": ["40", 0], "denoise": 1.0}},
        "60": {"class_type": "VAEDecode", "inputs": {"samples": ["50", 0], "vae": vae}},
        "10": {"class_type": "SaveImageWebsocket", "inputs": {"images": ["60", 0]}},
    })
    return wf



# ---- Qwen-Image 2.1 Turbo (11-17): one clean base model for making AND editing, plus an explicit add-on (a LoRA)
# switched on per request. Make and edit share loader nodes with identical ids and inputs, so ComfyUI's output cache
# keeps the model, text reader and VAE loaded between a make and an edit (no reload). The add-on is a LoraLoaderModelOnly
# in the graph only when asked for; off = not in the graph at all, so ordinary pictures use the clean base.
QWEN21_REPO = "Comfy-Org/Qwen-Image-2.1"
QWEN21_UNET = "qwen_image_2.1_turbo_int8_convrot.safetensors"      # diffusion_models/
QWEN21_VAE = "qwen_image_2.1_vae_bf16.safetensors"                 # vae/
QWEN21_TE_REPO = "pottokao/Qwen-Image-2.1-Text-Encoder-Heretic-W4A8"   # uncensored text reader (stock CLIPLoader)
QWEN21_TE = "qwen3vl_8b_w4a8_heretic.safetensors"
QWEN21_TE_FALLBACK = ("pottokao/Qwen-Image-2.1-Text-Encoder-Heretic", "qwen3vl_8b_bf16_heretic.safetensors")
QWEN21_SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"   # Turbo's own 8 steps
QWEN21_LORA_REPO = "chatrlabs/Qwen-Image-2.1-Uncensored-Omni-LoRA-v0.1"
QWEN21_LORA_FILE = "chatrlabs-omni-lora-v0.1-bf16.safetensors"
QWEN21_NAME = "Qwen-Image 2.1 Turbo"


def _qwen21_te() -> str | None:
    """The text reader file to load: the live-tuned one ("qwen21_te" in local.json) if it exists, else the W4A8
    heretic reader, else the bf16 one if downloaded. No automatic switch when a load fails (see _comfy_run)."""
    a, b = _snapshot(QWEN21_TE_REPO), _snapshot(QWEN21_TE_FALLBACK[0])
    named = _tune_text("qwen21_te", "")
    if named and any(x is not None and (x / named).is_file() for x in (a, b)):
        return named
    if a is not None and (a / QWEN21_TE).is_file():
        return QWEN21_TE
    if b is not None and (b / QWEN21_TE_FALLBACK[1]).is_file():
        return QWEN21_TE_FALLBACK[1]
    return None


def _qwen21_ready() -> bool:
    """Model file, VAE and a text reader are on this computer. Independent of QWEN_EDIT_ON: 2.1 is opt-in by choice."""
    q = _snapshot(QWEN21_REPO)
    return bool(q is not None and (q / "diffusion_models" / QWEN21_UNET).is_file()
                and (q / "vae" / QWEN21_VAE).is_file() and _qwen21_te() is not None)


def _qwen21_lora() -> tuple[str, float] | None:
    """(file, strength) of the explicit add-on, or None when its file isn't here. 11-21: the 'qwen-explicit-addon'
    ROLE in macros.json wins when set (reassignable from Settings, no rebuild); otherwise the live-tunable local.json
    path: qwen21_explicit_lora (file), qwen21_explicit_repo (Hugging Face repo), qwen21_explicit_strength."""
    role = _role_lora("qwen-explicit-addon")
    if role is not None:
        return role
    name = _tune_text("qwen21_explicit_lora", QWEN21_LORA_FILE)
    strength = max(0.0, min(2.0, _tune("qwen21_explicit_strength", 1.0)))
    snap = _snapshot(_tune_text("qwen21_explicit_repo", QWEN21_LORA_REPO))
    found = (COMFY_LORAS / name).is_file() or bool(snap is not None and (snap / name).is_file())
    return (name, strength) if found else None


def _qwen21_refs(prompt: str, n: int) -> str:
    """2.1 labels the photos <image1>, <image2>...: "photo 2" / "image 2" / "picture 2" in the words become <image2>
    (only for photos that exist). A label fix, not a rewrite; the same rule the 2511 editor applies ("Picture N")."""
    def fix(m):
        k = int(m.group(1))
        return f"<image{k}>" if k <= n else m.group(0)
    return re.sub(r"\b(?:photo|image|pic|picture)\s*#?\s*([1-3])\b", fix, prompt or "", flags=re.IGNORECASE)


def _qwen21_explicit(mode, brain_flag=None, base_recipe=None) -> tuple[str, float] | None:
    """The add-on to load, or None. mode: Make's "Auto"/"On"/"Off" (any case; None = not sent). brain_flag: the chat
    brain's "explicit" true/false (None = not sent). Auto: the brain's flag, else (a change to a picture) the
    explicit-model rule. Never decided from the words. Raises UserFacing when the add-on is wanted but not here.
    11-19: Qwen-Image 2.1 is an everyday model, so it is always safe: no add-on, unless phone_config lists it as an
    adult model."""
    if "qwen21" not in _adult_models():
        return None
    m = str(mode or "auto").lower()
    want = (m == "on") or (m == "auto" and (brain_flag is True
                                            or (brain_flag is None and (base_recipe or "") in QWEN_EXPLICIT_RECIPES)))
    if m == "off" or not want:
        return None
    lora = _qwen21_lora()
    if lora is None:
        raise UserFacing("The explicit add-on for Qwen-Image 2.1 isn't on your computer yet, so nothing was made. "
                         "Switch it Off, or pick another model.")
    return lora


def _qwen21_head(explicit_lora=None, loras=None, sigmas: str | None = None) -> tuple[dict, list]:
    """Nodes shared by make and edit (same ids, same inputs): "1" model, "2" text reader, "3" VAE, "7" sigmas, "8"
    sampler. The explicit add-on is node "5" (only when on); phone style add-ons chain as "20<i>"; "4" is Turbo's
    cache node, last before the guider. Returns (graph, model link)."""
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": QWEN21_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": _qwen21_te() or QWEN21_TE, "type": "qwen_image",
                                                     "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN21_VAE}},
    }
    model = ["1", 0]
    if explicit_lora:                                  # off = not in the graph at all
        name, strength = explicit_lora
        wf["5"] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": model, "lora_name": name, "strength_model": float(strength)}}
        model = ["5", 0]
    for i, (name, strength) in enumerate(loras or []):
        nid = f"20{i}"
        wf[nid] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": model, "lora_name": name, "strength_model": float(strength)}}
        model = [nid, 0]
    wf["4"] = {"class_type": "QwenImage21Cache", "inputs": {"model": model, "device": "auto", "dtype": "default"}}
    wf["7"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": sigmas or QWEN21_SIGMAS}}
    wf["8"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
    return wf, ["4", 0]


def _qwen21_workflow(prompt: str, w: int, h: int, seed: int, cfg: float = 1.0, negative: str = "",
                     explicit_lora=None, loras=None, start=None, sigmas: str | None = None) -> dict:
    """Qwen-Image 2.1 Turbo text to picture, as ComfyUI's own template builds it (no prompt enhancer): 8 fixed sigmas,
    euler, cfg 1. `start` = (photo file, strength 0.05..1, w, h): redraw that fraction of the photo (SplitSigmasDenoise
    keeps the later part of the 8 sigmas)."""
    w, h = max(256, w // 32 * 32), max(256, h // 32 * 32)
    wf, model = _qwen21_head(explicit_lora, loras, sigmas)
    wf.update({
        "20": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "prompt": prompt,
                                                                 "negative_prompt": negative or "", "resolution": 1024}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "11": {"class_type": "CFGGuider", "inputs": {"model": model, "positive": ["20", 0], "negative": ["20", 1],
                                                     "cfg": float(cfg)}},
        "12": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["11", 0],
                                                                 "sampler": ["8", 0], "sigmas": ["7", 0],
                                                                 "latent_image": ["6", 0]}},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImageWebsocket", "inputs": {"images": ["13", 0]}},
    })
    if start:
        image, strength, sw, sh = start
        wf["40"] = {"class_type": "LoadImage", "inputs": {"image": image}}
        wf["41"] = {"class_type": "ImageScale", "inputs": {"image": ["40", 0], "upscale_method": "lanczos",
                                                           "width": max(256, sw // 32 * 32),
                                                           "height": max(256, sh // 32 * 32), "crop": "center"}}
        wf["6"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["41", 0], "vae": ["3", 0]}}
        wf["14"] = {"class_type": "SplitSigmasDenoise", "inputs": {"sigmas": ["7", 0],
                                                                   "denoise": max(0.05, min(1.0, float(strength)))}}
        wf["12"]["inputs"]["sigmas"] = ["14", 1]
    return wf


def _qwen21_edit_workflow(prompt: str, images: list, seed: int, explicit_lora=None, resolution: int | None = None,
                          loras=None, reference_size: tuple | None = None, sigmas: str | None = None) -> dict:
    """Qwen-Image 2.1 Turbo photo edit with 1 to 3 photos (image 1 is the one being changed and sets the shape),
    using the same loaded model, reader and VAE as the pictures. `resolution` is the pixel budget per side
    (1024 = about 1 MP, 0 = each photo's own size). `reference_size` (w, h): a new picture that borrows from the
    photos, in its own shape, instead of the photo's."""
    wf, model = _qwen21_head(explicit_lora, loras, sigmas)
    n = len(images[:3])
    res = int(resolution if resolution is not None else _tune("qwen21_edit_resolution", 1024))
    enc = {"clip": ["2", 0], "vae": ["3", 0], "prompt": _qwen21_refs(prompt, n), "negative_prompt": "",
           "resolution": res}
    for i, name in enumerate(images[:3]):
        wf[f"3{i}0"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        enc[f"images.image_{i + 1}"] = [f"3{i}0", 0]
    latent = ["20", 2]                                 # the template: an empty latent on image 1's resized shape
    if reference_size:
        wf["6"] = {"class_type": "EmptyLatentImage", "inputs": {"width": max(256, reference_size[0] // 32 * 32),
                                                                 "height": max(256, reference_size[1] // 32 * 32),
                                                                 "batch_size": 1}}
        latent = ["6", 0]
    wf.update({
        "20": {"class_type": "TextEncodeQwenImage21", "inputs": enc},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "11": {"class_type": "CFGGuider", "inputs": {"model": model, "positive": ["20", 0], "negative": ["20", 1],
                                                     "cfg": 1.0}},
        "12": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["11", 0],
                                                                 "sampler": ["8", 0], "sigmas": ["7", 0],
                                                                 "latent_image": latent}},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImageWebsocket", "inputs": {"images": ["13", 0]}},
    })
    return wf


CHROMA_NEGATIVE = ("This low quality greyscale unfinished sketch is inaccurate and flawed. The image is very blurred "
                   "and lacks detail with excessive chromatic aberrations and artifacts. The image is overly saturated "
                   "with excessive bloom. It has a toony aesthetic with bold outlines and flat colors.")


def _chroma_workflow(prompt: str, negative: str, w: int, h: int, seed: int, steps: int = 26, cfg: float = 3.5,
                     sampler: str = "euler", scheduler: str = "beta", shift: float = 1.0,
                     loras: list | None = None) -> dict:
    """Chroma1-HD exactly as ComfyUI's own template builds it (fp8mixed model, T5 fp8 with padding off, CFGGuider)."""
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "Chroma1-HD-fp8mixed.safetensors",
                                                     "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "t5xxl_fp8_e4m3fn_scaled.safetensors",
                                                     "type": "chroma", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "T5TokenizerOptions", "inputs": {"clip": ["2", 0], "min_padding": 0, "min_length": 0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or CHROMA_NEGATIVE, "clip": ["4", 0]}},
        "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": float(shift)}},
        "8": {"class_type": "CFGGuider", "inputs": {"model": ["7", 0], "positive": ["5", 0], "negative": ["6", 0],
                                                    "cfg": float(cfg)}},
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler}},
        "11": {"class_type": "BasicScheduler", "inputs": {"model": ["7", 0], "scheduler": scheduler,
                                                          "steps": int(steps), "denoise": 1.0}},
        "12": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "13": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "14": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["12", 0], "guider": ["8", 0],
                                                                 "sampler": ["9", 0], "sigmas": ["11", 0],
                                                                 "latent_image": ["13", 0]}},
        "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImageWebsocket", "inputs": {"images": ["15", 0]}},
    }
    model = ["1", 0]
    for i, (name, strength) in enumerate(loras or []):
        nid = f"20{i}"
        wf[nid] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": model, "lora_name": name, "strength_model": float(strength)}}
        model = [nid, 0]
    wf["7"]["inputs"]["model"] = model
    return wf


def _sdxl_workflow(prompt: str, negative: str, w: int, h: int, seed: int, steps: int = 30, cfg: float = 5.0,
                   sampler: str = "dpmpp_2m_sde", scheduler: str = "karras", loras: list | None = None,
                   ckpt: str = "juggernautXL_ragnarok.safetensors") -> dict:
    """An SDXL checkpoint (Juggernaut XL Ragnarok) the plain ComfyUI way: one CheckpointLoaderSimple, a KSampler at
    30 steps, positive and negative text. LoRAs chain model+clip (both strengths = the LoRA's strength) between the
    loader and the text encoders. Node ids match paint and start-from (_START_POINTS["sdxl"])."""
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or "", "clip": ["1", 1]}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": seed, "steps": int(steps),
                                                   "cfg": float(cfg), "sampler_name": sampler, "scheduler": scheduler,
                                                   "positive": ["4", 0], "negative": ["5", 0],
                                                   "latent_image": ["6", 0], "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {"class_type": "SaveImageWebsocket", "inputs": {"images": ["9", 0]}},
    }
    model, clip = ["1", 0], ["1", 1]
    for i, (name, strength) in enumerate(loras or []):
        nid = f"20{i}"
        wf[nid] = {"class_type": "LoraLoader",
                   "inputs": {"model": model, "clip": clip, "lora_name": name,
                              "strength_model": float(strength), "strength_clip": float(strength)}}
        model, clip = [nid, 0], [nid, 1]
    wf["8"]["inputs"]["model"] = model
    wf["4"]["inputs"]["clip"] = clip
    wf["5"]["inputs"]["clip"] = clip
    return wf


# ---- Create screen: recipes the phone shows as a form (controls built from ComfyUI's own option lists)

SHAPES = {"square": (1024, 1024), "portrait": (832, 1216), "landscape": (1216, 832), "wallpaper": (704, 1472)}
COMFY_LORAS = ROOT / ".fixer-private"      # LoRA files, kept in a hidden store OUTSIDE the models/HF cache so nothing
                                           # else on this PC browses or indexes them; still size-capped (accounted below)
COMFY_VAE = MODELS / "comfy-vae"           # ae.safetensors, copied out of the old Z-Image repo (inside the capped folder)


def _ensure_lora_store() -> None:
    """Create the LoRA store and lock it down on Windows, idempotently, every start:
      - hidden+system, so it stays out of Explorer and default search;
      - EFS-encrypted (the directory is marked, so files created in it inherit encryption): only THIS Windows
        account can read the contents; every other account, administrators included, sees ciphertext;
      - an ACL restricted to this account + SYSTEM, so no other account can even list it.
    None of this blocks this account's own reads, so ComfyUI loads the files normally. Each step is best-effort:
    a failure never stops the store working (we are its owner). EFS keys live with this user's profile — back the
    cert up once (`cipher /x`) or a lost profile means lost LoRAs."""
    COMFY_LORAS.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        return
    d = str(COMFY_LORAS)

    def _run(cmd: list[str]) -> None:
        try:
            subprocess.run(cmd, check=False, creationflags=0x08000000,     # CREATE_NO_WINDOW: no console flash
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:                                                  # noqa: BLE001
            pass

    _run(["attrib", "+h", "+s", d])
    _run(["cipher", "/e", d])                      # mark the folder EFS-encrypted; new files inherit it
    user = os.environ.get("USERNAME") or ""
    if user:                                       # ACL: only this account + SYSTEM (*S-1-5-18, locale-safe)
        _run(["icacls", d, "/inheritance:r",
              "/grant:r", f"{user}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"])

CREATE_RECIPES = {
    "juggernaut": {
        "name": "Juggernaut XL Ragnarok", "kind": "image", "builder": "sdxl",
        "note": "Photo-real people, portraits and scenes, and fine with explicit pictures. Uses 'Leave out'. 30 steps.",
        "ckpt": "juggernautXL_ragnarok.safetensors",
        "needs": ["Kandoo/Juggernaut-XL-Ragnarok"],
        "file": ("Kandoo/Juggernaut-XL-Ragnarok", "juggernautXL_ragnarok.safetensors"),
        "defaults": {"steps": 30, "cfg": 5.0, "sampler": "dpmpp_2m_sde", "scheduler": "karras", "shift": 1.0},
        "negative": True, "default_negative": "bad eyes, cgi, airbrushed, plastic, deformed, watermark",
        "detail_pass": False, "uses_shift": False, "lora_family": "sdxl",
        "shapes": {"wallpaper": (768, 1344)},
        "cfg_help": "5 suits portraits, 6 suits everything else. Too high looks harsh.",
    },
    "chroma": {
        # Strong on fine natural detail and on explicit content alike; slower (26 steps with guidance).
        "name": "Chroma1-HD", "kind": "image", "note": "The finest natural detail (skin, hair, fabric, fur) and fine with explicit pictures. About three times slower. Uses 'Leave out'.", "builder": "chroma", "lora_family": "chroma",
        "needs": ["Comfy-Org/Chroma1-HD_repackaged", "comfyanonymous/flux_text_encoders"],
        "ready": lambda: (COMFY_VAE / "ae.safetensors").is_file(),   # ae.safetensors moved out of the Z-Image repo
        "file": ("Comfy-Org/Chroma1-HD_repackaged", "split_files/diffusion_models/Chroma1-HD-fp8mixed.safetensors"),
        "defaults": {"steps": 26, "cfg": 4.5, "sampler": "euler", "scheduler": "beta", "shift": 1.0},
        "negative": True,
        "detail_pass": False,                  # 26 steps already draw fine texture
        "cfg_help": "Also how hard it avoids 'Leave out'. Too high (8+) looks harsh and over-cooked.",
    },
    "qwen21": {
        # 11-17: Qwen-Image 2.1 Turbo. Opt-in until the owner's real use (the Queue screen's timings) shows it is at
        # least as good: it is NOT in AUTO_MODELS_DEFAULT. Makes and edits with the same loaded model.
        "name": QWEN21_NAME, "kind": "image", "builder": "qwen21", "lora_family": "qwen21",
        "note": "New. Follows long, exact descriptions and lettering well, and makes and edits photos with the same "
                "model, so nothing reloads between them. Explicit add-on below. 8 steps.",
        "needs": [QWEN21_REPO],
        "ready": lambda: _qwen21_ready(),
        "defaults": {"steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "shift": 1.0,
                     "explicit": "Auto"},
        "negative": False, "detail_pass": False, "uses_shift": False, "uses_scheduler": False,
        "uses_steps": False, "uses_sampler": False,           # the 8 sigmas are fixed (the template's note)
        "cfg_help": "Made for 1 (fastest). Above 1 takes about twice as long and changes little.",
    },
}


_START_POINTS = {   # builder -> (blank-canvas node, vae output, node whose denoise sets the strength)
    "chroma": ("13", ["3", 0], "11"),
    "sdxl": ("6", ["1", 2], "8"),
}


def _start_from(wf: dict, builder: str, image: str, w: int, h: int, strength: float):
    """Replaces the recipe's blank canvas with the photo (fitted to w x h) and redraws `strength` of it
    (0.2 = barely touched, 1.0 = only the rough layout survives)."""
    latent, vae, denoise_node = _START_POINTS[builder]
    wf["40"] = {"class_type": "LoadImage", "inputs": {"image": image}}
    wf["41"] = {"class_type": "ImageScale", "inputs": {"image": ["40", 0], "upscale_method": "lanczos",
                                                       "width": w, "height": h, "crop": "center"}}
    wf[latent] = {"class_type": "VAEEncode", "inputs": {"pixels": ["41", 0], "vae": vae}}
    wf[denoise_node]["inputs"]["denoise"] = max(0.05, min(1.0, float(strength)))


PAINT_STRENGTH = {"chroma": 0.9, "qwen21": 1.0, "sdxl": 0.8}   # "Auto" on the phone's Change slider


def _paint_crop(photo, mask_img):
    """Crop-and-stitch, step 1: the painted spot grown a little and feathered, plus context around it, scaled to
    about 1 MP so even a small spot is redrawn at full detail. Returns (crop, crop mask, box, soft full mask)."""
    from PIL import Image, ImageFilter
    W, H = photo.size
    hard = mask_img.convert("L").resize((W, H), Image.BILINEAR).point(lambda x: 255 if x > 127 else 0)
    if not hard.getbbox():
        raise UserFacing("Paint over the spot to change first.")
    grow = max(6, round(min(W, H) * 0.012))
    grown = hard.filter(ImageFilter.GaussianBlur(grow)).point(lambda x: 255 if x > 8 else 0)   # fast dilation
    soft = grown.filter(ImageFilter.GaussianBlur(max(3, grow // 2)))
    x0, y0, x1, y1 = soft.getbbox()
    bw, bh = x1 - x0, y1 - y0
    pad = int(max(bw, bh) * 0.5) + 32                     # enough surroundings for the model to match them
    cw, ch = min(W, bw + 2 * pad), min(H, bh + 2 * pad)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    left = max(0, min(W - cw, cx - cw // 2))
    top = max(0, min(H - ch, cy - ch // 2))
    box = (left, top, left + cw, top + ch)
    scale = min(4.0, (1_000_000 / max(1, cw * ch)) ** 0.5)
    tw, th = max(256, int(cw * scale) // 16 * 16), max(256, int(ch * scale) // 16 * 16)
    crop = photo.crop(box).resize((tw, th), Image.LANCZOS)
    crop_mask = soft.crop(box).resize((tw, th), Image.BILINEAR)
    return crop, crop_mask, box, soft


def _paint_workflow(r: dict, v: dict, w: int, h: int, seed: int, loras: list, crop_name: str, mask_name: str,
                    strength: float) -> dict:
    """The recipe's own graph redrawing only the masked part of the crop: the crop is encoded as the starting
    latent, SetLatentNoiseMask limits the noise to the spot, Differential Diffusion follows the soft edge."""
    builder = r["builder"]
    if builder == "qwen21":
        # The crop is image 1 of the 2.1 editor at its own size (resolution 0, so the mask lines up); only the masked
        # part is redrawn, at full strength (the 8 sigmas are fixed).
        wf = _qwen21_edit_workflow(v.get("prompt") or "", [crop_name], seed,
                                   _qwen21_explicit(v.get("explicit"), None, None), resolution=0, loras=loras)
        wf["42"] = {"class_type": "LoadImageMask", "inputs": {"image": mask_name, "channel": "red"}}
        wf["43"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["300", 0], "vae": ["3", 0]}}
        wf["6"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["43", 0], "mask": ["42", 0]}}
        wf["12"]["inputs"]["latent_image"] = ["6", 0]
        wf["44"] = {"class_type": "DifferentialDiffusion", "inputs": {"model": wf["11"]["inputs"]["model"],
                                                                      "strength": 1.0}}
        wf["11"]["inputs"]["model"] = ["44", 0]
        return wf
    wf = _recipe_workflow(r, {**v, "detail": "Off"}, w, h, seed, loras)
    latent, vae, denoise_node = _START_POINTS[builder]
    wf["40"] = {"class_type": "LoadImage", "inputs": {"image": crop_name}}
    wf["42"] = {"class_type": "LoadImageMask", "inputs": {"image": mask_name, "channel": "red"}}
    wf["43"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["40", 0], "vae": vae}}
    wf[latent] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["43", 0], "mask": ["42", 0]}}
    wf["44"] = {"class_type": "DifferentialDiffusion", "inputs": {"model": wf["8"]["inputs"]["model"],
                                                                  "strength": 1.0}}
    wf["8"]["inputs"]["model"] = ["44", 0]
    wf[denoise_node]["inputs"]["denoise"] = max(0.05, min(1.0, float(strength)))
    return wf


def _paint_picture(r, v, seed, loras, photo, mask_b64, emit, cancel):
    """Paint to change: the picture's own model redraws only the painted spot; every other pixel is kept."""
    import uuid
    from PIL import Image
    crop, crop_mask, box, soft = _paint_crop(photo, _image_in(mask_b64))
    if r.get("builder") == "qwen21":                       # 2.1 reads in 32s: keep the crop and its mask lined up
        nw, nh = max(256, crop.width // 32 * 32), max(256, crop.height // 32 * 32)
        if (nw, nh) != crop.size:
            crop, crop_mask = crop.resize((nw, nh), Image.LANCZOS), crop_mask.resize((nw, nh), Image.BILINEAR)
    tag = uuid.uuid4().hex
    crop_name, mask_name = f"pc_{tag}.png", f"pm_{tag}.png"
    folder = COMFY_PRIVATE / "input"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        crop.save(folder / crop_name, format="PNG")
        crop_mask.convert("RGB").save(folder / mask_name, format="PNG")
        builder = r["builder"]
        strength = v.get("strength")
        strength = float(strength) if strength not in (None, "") else PAINT_STRENGTH.get(builder, 0.9)
        wf = _paint_workflow(r, v, crop.width, crop.height, seed, loras, crop_name, mask_name, strength)
        png = asyncio.run(_comfy_run(wf, emit, cancel, previews=True))
    finally:
        (folder / crop_name).unlink(missing_ok=True)
        (folder / mask_name).unlink(missing_ok=True)
    if cancel.is_set() or not png:
        return
    patch = Image.open(io.BytesIO(png)).convert("RGB").resize((box[2] - box[0], box[3] - box[1]), Image.LANCZOS)
    out = photo.copy()
    out.paste(patch, box[:2], soft.crop(box))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    emit({"t": "image", "v": base64.b64encode(buf.getvalue()).decode(), "seed": seed, "index": 0})
    emit({"t": "progress", "v": 1.0})


WAN14_REPO = "Comfy-Org/Wan_2.2_ComfyUI_Repackaged"
WAN_TE_REPO = "Comfy-Org/Wan_2.1_ComfyUI_repackaged"
WAN14_FILES = {
    "high": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
    "low": "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
    "lora_high": "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
    "lora_low": "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors",
    "vae": "wan_2.1_vae.safetensors",
    "te": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
}
# Explicit-motion add-on (community "NSFW-22", one file per expert). Loaded only when the request uses explicit
# words (phone_config.json "explicit_pattern", editable without an app update), so ordinary clips are untouched.
WAN_EXPLICIT_REPO = "lopi999/Wan2.2-I2V_General-NSFW-LoRA"
WAN_EXPLICIT = ("NSFW-22-H-e8.safetensors", "NSFW-22-L-e8.safetensors", "nsfwsks")   # high, low, trigger word
EXPLICIT_PATTERN_DEFAULT = (r"\b(nsfw|nude|nudes|nudity|naked|topless|bottomless|explicit|porn\w*|sex|sexy|sexual\w*|"
                            r"erotic\w*|lewd|undress\w*|breasts?|boobs?|tits|nipples?|pussy|vagina|penis|cock|"
                            r"genitals?|blowjob|handjob|fuck\w*|masturbat\w*|orgasm\w*|cum|cums|cumming|cumshot)\b")


def _is_explicit(text: str) -> bool:
    """True when the words are explicit (phone_config "explicit_pattern", editable without an app update). Always
    False inside a job the phone sent with Explicit off (11-19), so nothing explicit is picked from the words."""
    if getattr(_TL, "safe", False):
        return False
    return _words_explicit(text)


def _words_explicit(text: str) -> bool:
    """The explicit-words check itself, with no regard to the job's safe flag (add-on names, 11-19)."""
    import re
    try:
        cfg = json.loads(PHONE_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    try:
        return bool(re.search(cfg.get("explicit_pattern") or EXPLICIT_PATTERN_DEFAULT, text or "", re.IGNORECASE))
    except re.error:
        return False


# The chat picks models by itself (the owner 11-17: chat is "just another way"; the Make tab is where the choosing happens). Editable
# in phone_config "auto_models"; a missing or not-installed choice falls back down each list.
AUTO_MODELS_DEFAULT = {
    "picture_explicit": ["chroma", "juggernaut"],
    "picture": ["juggernaut", "chroma"],
    "picture_safe": ["qwen21"],                         # 11-19: with 18+ off, only the everyday models
}

# 11-19 (the owner: "the sfw work models will be the non chroma or juggernaut models, so they should always be
# safe"): the 18+ picture models. Every other model is an everyday one: it never loads an explicit add-on or an 18+
# LoRA, whatever the switch says. phone_config "adult_models" may override the list.
ADULT_MODELS_DEFAULT = ("chroma", "juggernaut")


def _adult_models() -> set:
    try:
        cfg = json.loads(PHONE_CONFIG.read_text(encoding="utf-8")).get("adult_models")
    except Exception:
        cfg = None
    return set(cfg) if isinstance(cfg, list) and cfg else set(ADULT_MODELS_DEFAULT)


def _auto_models() -> dict:
    try:
        cfg = json.loads(PHONE_CONFIG.read_text(encoding="utf-8")).get("auto_models") or {}
    except Exception:
        cfg = {}
    return {**AUTO_MODELS_DEFAULT, **cfg}


def _first_ready(ids: list) -> dict | None:
    for rid in ids or []:
        r = CREATE_RECIPES.get(rid)
        if r and _recipe_ready(r):
            return r
    return None


def _picture_recipe(p: dict) -> dict | None:
    """The recipe for a chat picture: the phone's explicit choice (newer builds send "model_choice"), otherwise
    chosen from the brain's "explicit" flag (or, on old phones, the words): explicit -> the explicit model, anything
    else -> the default picture model. A saved choice that is gone (bigASP, FLUX klein) falls through to this."""
    choice = p.get("model_choice")
    a = _auto_models()
    if p.get("safe") is True:                          # 11-19: 18+ off -> an everyday model, never an 18+ one
        if choice and choice != "auto" and choice not in _adult_models():
            r = CREATE_RECIPES.get(choice)
            if r and _recipe_ready(r):
                return r
        return _first_ready([x for x in a.get("picture_safe") or [] if x not in _adult_models()])
    if choice and choice != "auto":
        r = CREATE_RECIPES.get(choice) or _chat_recipe(choice)
        if r and _recipe_ready(r):
            return r
    flag = p.get("explicit")                           # 11-17: the brain's own call (newer phones); old ones: the words
    explicit = flag is True or _is_explicit(p.get("prompt") or "")     # only a true flag adds to the words; false = "not said"
    return _first_ready(a["picture_explicit"] if explicit else a["picture"])


def _explicit_video(prompt: str, mode: str | None = None) -> float:
    """Strength for the explicit add-on (0 = not loaded). The phone's "Explicit videos" switch decides: "on" every
    video, "off" never, "auto" (and builds without the switch) only when the request's words are explicit."""
    import re
    try:
        cfg = json.loads(PHONE_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    snap = _snapshot(WAN_EXPLICIT_REPO)
    if snap is None or not all((snap / f).is_file() for f in WAN_EXPLICIT[:2]):
        return 0.0
    mode = str(mode or "auto").lower()
    if mode == "off":
        return 0.0
    if mode != "on" and not _is_explicit(prompt):
        return 0.0
    return max(0.0, min(2.0, float(cfg.get("video_explicit_strength", 1.0))))


COMFY_VIDEO = os.environ.get("FIXER_COMFY_VIDEO", "1") != "0"         # 0 = back to the 5B diffusers video
WAN14_AREA = int(os.environ.get("FIXER_WAN_AREA", str(832 * 480)))   # pixels per frame; 1280*720 = HD, ~2.5x slower
WAN14_FPS = 16                                                         # trained rate: 81 frames = 5 s
WAN14_PART = 81                                                        # 11-18: frames per part of a longer clip (4k+1)


def _video_parts(seconds, max_seconds: float | None = None) -> tuple[int, list[int]]:
    """11-18: (frames in the whole clip, frames per part) for a clip of `seconds`. Wan 2.2 is trained on 81-frame
    (5 s) clips, so a longer clip is made as parts of at most 81 frames, each starting from the previous part's last
    frame; that shared frame is dropped when joining, so the clip has 1 + sum(part - 1) frames. Every part is 4k+1
    frames (the model's own rule), the parts are as even as possible (no tiny last part), and the length is clamped to
    1 s .. video_max_seconds (local.json knob, default 30)."""
    top = max(5.0, float(_tune("video_max_seconds", 30) if max_seconds is None else max_seconds))
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        s = 5.0
    s = max(1.0, min(top, s if s == s else 5.0))         # (s == s: not NaN)
    total = int(round(s * WAN14_FPS / 4)) * 4 + 1
    units = (total - 1) // 4                             # 4-frame steps after the first frame
    per = (WAN14_PART - 1) // 4
    n = max(1, -(-units // per))
    base, extra = divmod(units, n)
    return total, [(base + (1 if i < extra else 0)) * 4 + 1 for i in range(n)]


def _part_seed(seed: int, i: int) -> int:
    """11-18: part 1 keeps the job's seed (a 5 s clip is unchanged); later parts derive theirs from it, so a fixed
    seed still reproduces the whole clip."""
    return (int(seed) + i * 0x9E3779B1) % (2 ** 32)


def _wan14_ready() -> bool:
    w, te = _snapshot(WAN14_REPO), _snapshot(WAN_TE_REPO)
    if w is None or te is None:
        return False
    sf = w / "split_files"
    return all((sf / d / WAN14_FILES[k]).is_file() for k, d in
               (("high", "diffusion_models"), ("low", "diffusion_models"), ("lora_high", "loras"),
                ("lora_low", "loras"), ("vae", "vae"))) \
        and (te / "split_files" / "text_encoders" / WAN14_FILES["te"]).is_file()


def _wan14_workflow(prompt: str, image: str, w: int, h: int, length: int, seed: int, steps: int = 4,
                    split: int = 2, shift: float = 5.0, explicit: float = 0.0, loras: list | None = None) -> dict:
    """Wan 2.2 image-to-video A14B as ComfyUI's own template builds it: the high-noise expert draws the motion for the
    first half of the steps, the low-noise expert the detail for the rest, each with its lightx2v 4-step LoRA, cfg 1.
    `loras` are [(high, low, strength)] pairs, chained after the explicit add-on, high on the high expert, low on low."""
    f = WAN14_FILES
    if explicit > 0:
        prompt = f"{WAN_EXPLICIT[2]}, {prompt}"
    wf = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": f["te"], "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": f["vae"]}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": f["high"], "weight_dtype": "default"}},
        "5": {"class_type": "UNETLoader", "inputs": {"unet_name": f["low"], "weight_dtype": "default"}},
        "6": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["4", 0], "lora_name": f["lora_high"],
                                                              "strength_model": 1.0}},
        "7": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["5", 0], "lora_name": f["lora_low"],
                                                              "strength_model": 1.0}},
        "8": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["6", 0], "shift": float(shift)}},
        "9": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["7", 0], "shift": float(shift)}},
        "10": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": ""}},
        "12": {"class_type": "WanImageToVideo", "inputs": {"positive": ["10", 0], "negative": ["11", 0],
                                                           "vae": ["3", 0], "width": w, "height": h,
                                                           "length": length, "batch_size": 1,
                                                           "start_image": ["1", 0]}},
        "13": {"class_type": "KSamplerAdvanced", "inputs": {
            "model": ["8", 0], "add_noise": "enable", "noise_seed": seed, "steps": steps, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "positive": ["12", 0], "negative": ["12", 1],
            "latent_image": ["12", 2], "start_at_step": 0, "end_at_step": split,
            "return_with_leftover_noise": "enable"}},
        "14": {"class_type": "KSamplerAdvanced", "inputs": {
            "model": ["9", 0], "add_noise": "disable", "noise_seed": 0, "steps": steps, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "positive": ["12", 0], "negative": ["12", 1],
            "latent_image": ["13", 0], "start_at_step": split, "end_at_step": steps,
            "return_with_leftover_noise": "disable"}},
        "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["3", 0]}},
        "16": {"class_type": "SaveImageWebsocket", "inputs": {"images": ["15", 0]}},
    }
    if explicit > 0:                                   # each expert gets its own half of the add-on, after lightx2v
        wf["60"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["6", 0], "lora_name": WAN_EXPLICIT[0],
                                                                    "strength_model": float(explicit)}}
        wf["70"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["7", 0], "lora_name": WAN_EXPLICIT[1],
                                                                    "strength_model": float(explicit)}}
        wf["8"]["inputs"]["model"] = ["60", 0]
        wf["9"]["inputs"]["model"] = ["70", 0]
    high_model, low_model = wf["8"]["inputs"]["model"], wf["9"]["inputs"]["model"]
    for i, (hi, lo, strength) in enumerate(loras or []):   # user LoRA pairs, after the explicit add-on
        if hi:
            wf[f"80{i}"] = {"class_type": "LoraLoaderModelOnly",
                            "inputs": {"model": high_model, "lora_name": hi, "strength_model": float(strength)}}
            high_model = [f"80{i}", 0]
        if lo:
            wf[f"90{i}"] = {"class_type": "LoraLoaderModelOnly",
                            "inputs": {"model": low_model, "lora_name": lo, "strength_model": float(strength)}}
            low_model = [f"90{i}", 0]
    wf["8"]["inputs"]["model"], wf["9"]["inputs"]["model"] = high_model, low_model
    return wf


def _wan14_size(w: int, h: int) -> tuple[int, int]:
    """The photo's shape at WAN14_AREA pixels, sides divisible by 16."""
    ar = h / max(1, w)
    return (max(256, int(round((WAN14_AREA / ar) ** 0.5)) // 16 * 16),
            max(256, int(round((WAN14_AREA * ar) ** 0.5)) // 16 * 16))


def _room_for_pictures() -> None:
    """11-17: the MoE brain keeps its card share (ComfyUI reserves it). Only the dense brain still takes turns."""
    if LLAMA.running() and LLAMA.on_gpu and not LLAMA.moe:
        LLAMA.stop()
    if _tune("picture_ram_gb", 0) > 0 and LLAMA.moe and _ram()[0] < _tune("picture_ram_gb", 0):
        COMFY.free()                        # drop cached picture models first; the brain only if still short
        if _ram()[0] < _tune("picture_ram_gb", 0):
            LLAMA.pause("picture", lambda s: None)


def _brain_ram_gb() -> float:
    """What the MoE brain takes from RAM when it starts: its weights file (the card share is small)."""
    choice = gguf_choice(LAST_BRAIN) if _is_moe(LAST_BRAIN) else None
    return sum(z for _, z in choice[0]) / 1024 ** 3 if choice else 0.0


def _room_for_video(status) -> None:
    """11-17: Wan 2.2 14B holds ~38 GB of RAM in ComfyUI; the MoE brain's experts ~17 GB; 64 GB can't always hold
    both plus Windows. Check what is free; first drop ComfyUI's cached picture models, then (only if still short)
    pause the brain for this video and say so. Dense brain: stops as before (it can't share the card with a video)."""
    if not _is_moe(LLAMA.repo or LAST_BRAIN):
        if LLAMA.running() and LLAMA.on_gpu:
            status("Freeing memory…")
            LLAMA.stop()
        return
    if LLAMA.paused_for:                 # the video before this one already made room; keep it that way
        return
    need = _tune("video_ram_gb", 42)
    brain_gb = 0.0 if LLAMA.running() else _brain_ram_gb()      # an idle brain would come back mid-video
    if _ram()[0] - brain_gb >= need:
        return
    COMFY.free()
    if _ram()[0] - brain_gb >= need:
        return
    LLAMA.pause("video", status)


def job_comfy_video(p: dict, emit, cancel):
    VIDEO_BUSY.set()                                   # a chat meanwhile answers from RAM, not the full card
    try:
        _comfy_video(p, emit, cancel)
    finally:
        VIDEO_BUSY.clear()
        if LLAMA.running() and not LLAMA.on_gpu:
            LLAMA.stop()                               # next chat starts on the card again


def _comfy_video(p: dict, emit, cancel):
    """A clip of 1-30 s ("seconds", default 5) with Wan 2.2 A14B in ComfyUI. From a photo it animates that photo; from
    words alone it first draws the opening frame with a picture model, then animates it (the 14B model only works from
    a picture). 11-18: longer than 5 s = 5-second parts, each continuing from the previous part's last frame (see
    _video_parts); same words, add-ons, strength and size for every part, a seed per part derived from the job's.
    Frames are encoded part by part, so peak RAM is one part's PNGs (~40-80 MB) plus the MP4 so far."""
    import uuid
    import numpy as np
    from PIL import Image
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    prompt = p.get("prompt") or "natural motion, the camera holds steady"
    seed = int(p.get("seed") or int.from_bytes(os.urandom(4), "big")) % (2 ** 32)
    total, parts = _video_parts(p.get("seconds", 5))
    n = len(parts)
    _room_for_video(status)
    if _worker_alive():
        WORKER.stop()
    COMFY.ensure_current(status)
    name = f"vid_{uuid.uuid4().hex}.png"
    folder = COMFY_PRIVATE / "input"
    folder.mkdir(parents=True, exist_ok=True)
    names = [name]                                     # every start picture written for this job: all deleted below
    writer = _Mp4Writer(WAN14_FPS)
    mp4 = b""
    try:
        if p.get("image"):
            start = _image_in(p["image"])
            w, h = _wan14_size(*start.size)
        else:
            w, h = _wan14_size(16, 9) if p.get("aspect") != "portrait" else _wan14_size(9, 16)
            a = _auto_models()
            if p.get("safe") is True:                   # 11-19: 18+ off -> the everyday model draws the first frame
                first = _first_ready([x for x in a.get("picture_safe") or [] if x not in _adult_models()])
            else:
                first = (_first_ready(a["picture_explicit"]) if _explicit_video(prompt, p.get("explicit")) > 0
                         else _first_ready(a["picture"]))
            if first is None:
                raise UserFacing("No picture model is on your computer to draw the first frame.")
            v = {**first["defaults"], "prompt": prompt, "detail": "Off"}
            fw, fh = (1344, 768) if p.get("aspect") != "portrait" else (768, 1344)   # ~1 MP in the clip's shape
            status("Drawing the first frame…")
            png = asyncio.run(_comfy_run(_recipe_workflow(first, v, fw, fh, seed), lambda o: None, cancel))
            if cancel.is_set() or not png:
                return
            start = Image.open(io.BytesIO(png)).convert("RGB")
        start.resize((w, h), Image.LANCZOS).save(folder / name, format="PNG")
        del start
        status("Loading the video model…")
        strength = _explicit_video(prompt, p.get("explicit"))
        vloras, vtrig = _job_loras(p.get("loras"), "wan22")
        words = _with_triggers(prompt, vtrig)
        # count = every frame of the clip: the timing averages are per frame, so a 30 s clip's estimate scales with it
        _timing(model="Wan 2.2 14B", explicit=strength > 0, mp=w * h / 1e6, steps=4, count=total)
        weights = [x / sum(parts) for x in parts]     # one bar over the whole job, each part by its share of frames
        shown = [0.0]

        def bar(v: float):
            v = round(min(0.98, v), 3)                 # 1.0 only once the clip is finished
            if v > shown[0]:
                shown[0] = v
                emit({"t": "progress", "v": v})

        cur = name
        for i, length in enumerate(parts):
            if cancel.is_set():
                writer.discard()
                return
            label = "Creating video…" if n == 1 else f"Creating video… part {i + 1} of {n}"
            if i:
                status(label)
            done_w = sum(weights[:i])

            def part(o, _done=done_w, _w=weights[i]):
                if o.get("t") != "progress":
                    emit(o)
                elif o.get("node") in ("13", "14"):    # the two experts: first and second half of this part's share
                    bar((_done + _w * ((0.0 if o["node"] == "13" else 0.5) + o["v"] * 0.5)) * 0.96)
            wf = _wan14_workflow(words, cur, w, h, length, _part_seed(seed, i), explicit=strength, loras=vloras)
            frames = asyncio.run(_comfy_run(wf, part, cancel, frames=True, label=label))
            (folder / cur).unlink(missing_ok=True)     # this part's start picture is not needed any more
            if cancel.is_set() or not frames:
                writer.discard()
                return
            if i + 1 < n:                              # the next part starts from this part's last frame
                nxt = f"vid_{uuid.uuid4().hex}.png"
                names.append(nxt)
                last = Image.open(io.BytesIO(frames[-1])).convert("RGB")
                (last if last.size == (w, h) else last.resize((w, h), Image.LANCZOS)).save(folder / nxt, format="PNG")
                del last
                cur = nxt
            for b in frames[1 if i else 0:]:           # a later part's first frame repeats the previous last one
                writer.add(np.asarray(Image.open(io.BytesIO(b)).convert("RGB")))
            del frames
            bar((done_w + weights[i]) * 0.96)
        if cancel.is_set():
            writer.discard()
            return
        status("Finishing video…")
        made = writer.n
        mp4 = writer.finish()
    except BaseException:
        writer.discard()
        raise
    finally:
        for x in names:
            (folder / x).unlink(missing_ok=True)
    if cancel.is_set() or not mp4:
        return
    emit({"t": "progress", "v": 1.0})
    emit({"t": "video", "v": base64.b64encode(mp4).decode(), "seconds": round((made - 1) / WAN14_FPS, 2),
          "width": w, "height": h, "seed": seed})


# ---- Songs (11-20, the owner: "add a music maker to the maker. Give prompts and it develops a song"). ACE-Step 1.5
# turbo in ComfyUI (its own all-in-one file: the song model, its two text readers and its audio decoder). The chat
# model first turns the idea into style tags and lyrics (the prompt writer's slot, never the chat's), then ACE-Step
# sings it. 8 steps; about a minute of card for a 2-minute song.
SONG_REPO = "Comfy-Org/ACE-Step-1.5"
SONG_FILE = "checkpoints/ace_step_1.5_turbo_aio.safetensors"
SONG_CKPT = "ace_step_1.5_turbo_aio.safetensors"
SONG_SECONDS = (15, 360)
SONG_AUTO_DEFAULT = 150                                # Auto length when the writer gives none and there are no lyrics
SONG_KEYS = [f"{root} {q}" for q in ("major", "minor")
             for root in ("C", "C#", "Db", "D", "D#", "Eb", "E", "F", "F#", "Gb", "G", "G#", "Ab", "A", "A#", "Bb", "B")]
# The singing languages the phone offers (ACE-Step 1.5 knows more; these are the ones it sings well).
SONG_LANGS = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
              "nl": "Dutch", "sv": "Swedish", "pl": "Polish", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish",
              "ar": "Arabic", "hi": "Hindi", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "yue": "Cantonese",
              "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "tl": "Tagalog"}
ALBUM_TRACKS = (2, 12)


def _song_ready() -> bool:
    snap = _snapshot(SONG_REPO)
    return snap is not None and (snap / SONG_FILE).is_file()


def _song_workflow(tags: str, lyrics: str, seconds: float, seed: int, bpm: int, key: str,
                   language: str = "en", reference: str | None = None, keep: float = 0.45,
                   match: str = "start", revoice: float = 0.6) -> dict:
    """The ComfyUI template's graph (audio_ace_step_1_5_checkpoint), saved as MP3 and read back by _comfy_run.
    11-20 `reference` (a file in ComfyUI's private input folder): the recording is the starting point. The song is
    still planned from these tags and lyrics (the model's audio codes), then drawn starting from the recording with
    `keep` of it left (denoise 1 - keep): more keep stays closer to the recording's melody, groove and sound.
    (The first version used ACE-Step's reference input; in ComfyUI that switches off the audio codes, so the
    owner's lyrics, picks and artists were all ignored as soon as a link was added.)"""
    wf = _song_graph(tags, lyrics, seconds, seed, bpm, key, language)
    if reference:
        wf["9"] = {"class_type": "LoadAudio", "inputs": {"audio": reference}}
        wf["10"] = {"class_type": "TrimAudioDuration", "inputs": {"audio": ["9", 0], "start_index": 0.0,
                                                                   "duration": float(seconds)}}
        wf["11"] = {"class_type": "VAEEncodeAudio", "inputs": {"audio": ["10", 0], "vae": ["1", 2]}}
        if match in ("start", "both"):                 # the recording is where the song starts
            wf["6"]["inputs"]["latent_image"] = ["11", 0]
            wf["6"]["inputs"]["denoise"] = round(1.0 - max(0.1, min(0.8, keep)), 2)
        if match in ("voice", "both"):                 # ACE-Step's own voice-and-sound reference
            wf["3"]["inputs"]["generate_audio_codes"] = False   # ComfyUI drops the codes with a reference anyway
            wf["12"] = {"class_type": "ReferenceTimbreAudio", "inputs": {"conditioning": ["3", 0], "latent": ["11", 0]}}
            wf["6"]["inputs"]["positive"] = ["12", 0]
        if match == "revoice":
            # 11-20: two passes. The first sings the lyrics as planned (the audio codes on); the second redraws that
            # song from part way with the recording's voice and sound (ACE-Step's reference), so the words stay.
            wf["12"] = {"class_type": "ReferenceTimbreAudio", "inputs": {"conditioning": ["3", 0], "latent": ["11", 0]}}
            wf["13"] = {"class_type": "KSampler",
                        "inputs": {"model": ["2", 0], "positive": ["12", 0], "negative": ["4", 0],
                                   "latent_image": ["6", 0], "seed": seed + 1, "steps": 8, "cfg": 1.0,
                                   "sampler_name": "euler", "scheduler": "simple",
                                   "denoise": round(max(0.2, min(0.9, revoice)), 2)}}
            wf["7"]["inputs"]["samples"] = ["13", 0]
    return wf


# How much of a recording a song keeps. 11-20, tuned by ear with the owner: from 0.5 up the original singer's voice
# survives (0.45 sounded like a different singer); 0.55 kept her voice with 73% of the new words clear, 0.6 only 46%.
SONG_KEEP = {"loose": 0.3, "balanced": 0.45, "close": 0.55}


def _keep_of(p: dict) -> float:
    """The named strength, or an exact one (0.1-0.8) for tuning from this PC."""
    try:
        if p.get("reference_keep_value") is not None:
            return max(0.1, min(0.8, float(p["reference_keep_value"])))
    except (TypeError, ValueError):
        pass
    return SONG_KEEP.get(str(p.get("reference_keep") or "balanced"), SONG_KEEP["balanced"])


def _song_graph(tags: str, lyrics: str, seconds: float, seed: int, bpm: int, key: str, language: str) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": SONG_CKPT}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "3": {"class_type": "TextEncodeAceStepAudio1.5",
              "inputs": {"clip": ["1", 1], "tags": tags, "lyrics": lyrics, "seed": seed, "bpm": bpm,
                         "duration": float(seconds), "timesignature": "4",
                         "language": language if language in SONG_LANGS else "en", "keyscale": key,
                         "generate_audio_codes": True, "cfg_scale": 2.0, "temperature": 0.85, "top_p": 0.9,
                         "top_k": 0, "min_p": 0.0}},
        "4": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["3", 0]}},
        "5": {"class_type": "EmptyAceStep1.5LatentAudio", "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "6": {"class_type": "KSampler",
              "inputs": {"model": ["2", 0], "positive": ["3", 0], "negative": ["4", 0], "latent_image": ["5", 0],
                         "seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveAudioMP3",
              "inputs": {"audio": ["7", 0], "filename_prefix": "fixer-song/song", "quality": "V0"}},
    }


def _song_seconds(v) -> int | None:
    """The asked length in seconds, or None for Auto (0, "auto", missing)."""
    if v in (None, "", 0, "auto"):
        return None
    try:
        return max(SONG_SECONDS[0], min(SONG_SECONDS[1], int(v)))
    except (TypeError, ValueError):
        return None


def _lyrics_seconds(lyrics: str) -> int:
    """Auto length from the lyrics: about 4 s a sung line, plus room for an intro, breaks and an outro."""
    lines = [x for x in lyrics.splitlines() if x.strip() and not x.strip().startswith("[")]
    return max(60, min(300, 25 + 4 * len(lines)))


def _artists_text(artists) -> str:
    """11-20 (the owner: artists as "inspiration and sound and voice"): the phone's picks, found on MusicBrainz, as
    "Name (genre, genre); ..." for the writer. At most 5, plain text only."""
    out = []
    for a in (artists if isinstance(artists, list) else [])[:5]:
        if isinstance(a, str):
            a = {"name": a}
        if not isinstance(a, dict):
            continue
        name = " ".join(str(a.get("name") or "").split())[:80]
        if not name:
            continue
        g = [" ".join(str(x).split())[:40] for x in (a.get("genres") or [])[:6] if str(x).strip()]
        out.append(name + (f" ({', '.join(g)})" if g else ""))
    return "; ".join(out)


def _inspired(artists: str) -> str:
    return (f" Inspired by: {artists}. Capture their sound in the tags in plain musical words: genre and era, "
            "signature instruments, production and rhythm. Describe their lead singer's voice in \"voice\" as exactly "
            "as you can: the range, the timbre, the texture, the way they deliver a line, their accent (e.g. for a "
            'deep country storyteller: "male, deep bass-baritone, resonant, gravelly, steady talk-sung delivery, '
            'little vibrato, Southern drawl"). Never put an artist\'s name in the tags, voice, title or lyrics, and '
            "never copy their lyrics.") if artists else ""


def _song_system(seconds: int | None, instrumental: bool, has_lyrics: bool, safe: bool | str, style: str = "",
                 language: str = "en", album: str = "", artists: str = "") -> str:
    lines = max(8, min(60, (seconds or 180) // 4))
    lyr = ('"[Instrumental]"' if instrumental else
           "the owner's lyrics exactly as given (only add section labels if they have none)" if has_lyrics else
           (f"new lyrics, about {lines} short singable lines in all" if seconds else
            "new lyrics for a full song (two or three verses, a chorus that comes back, a bridge, an outro)") +
           ", with section labels on their own lines ([Intro], [Verse 1], [Chorus], [Verse 2], [Bridge], [Outro])")
    lang = SONG_LANGS.get(language, "English")
    parts = ["You write songs for a music model. From the owner's idea, answer with one JSON object and nothing else: "
             '{"title": a short song title, '
             '"tags": 8-20 comma-separated style tags: genre, era, mood, lead instruments, rhythm feel, vocal type '
             '(e.g. "female vocal", "male vocal", "duet", "choir") and production, '
             + ('' if instrumental else
                '"voice": the lead voice in 6-12 comma-separated musical words: gender, range (e.g. bass-baritone, '
                'tenor, alto, soprano), timbre and texture (e.g. deep, resonant, gravelly, breathy, nasal, warm), '
                'delivery (e.g. talk-sung, belted, crooned, laid-back, staccato), vibrato and accent, ')
             + f'"lyrics": {lyr}, '
             '"bpm": a whole number 60-180 that suits the style, '
             '"key": a key like "A minor" or "Eb major"'
             + (', "seconds": the song\'s length in seconds (60-300) that suits its structure' if not seconds else "")
             + "}. Keep any names, places, phrases and lines the owner gave."]
    if style:
        parts.append(f" The owner chose this sound; start the tags from it and keep all of it: {style[:600]}.")
    if album:
        parts.append(f" This song is one track of an album; it shares the album's sound: {album[:600]}.")
    parts.append(_inspired(artists))
    if not instrumental and not has_lyrics and lang != "English":
        parts.append(f" Write the lyrics in {lang} (the title too); the tags stay in English.")
    parts.append(LYRIC_RULES.get(safe if isinstance(safe, str) else ("clean" if safe else "strong"), ""))
    return "".join(parts)


# 11-20 (the owner: "a toggle for explicit lyrics on or off"). The song form's Explicit lyrics switch, with the
# session's 18+ switch: off = clean; on with 18+ off = strong language but nothing sexual; on with 18+ on = explicit.
LYRIC_RULES = {
    "clean": " Keep the lyrics clean: no swearing, nothing sexual or explicit.",
    "strong": " Strong language and mature themes are fine where they suit the song; nothing sexual.",
    "explicit": " Explicit lyrics are fine: strong language and adult, sexual themes where they suit the song.",
}


def _lyric_rating(p: dict) -> str:
    if p.get("explicit_lyrics") is not True:
        return "clean"
    return "strong" if p.get("safe") is True else "explicit"


def _llm_json(system: str, text: str, cancel, max_tokens: int = 1800, temperature: float = 0.8) -> dict | None:
    """One JSON object from the chat model in the prompt writer's slot (never the chat's); None when it can't."""
    try:
        port = _enhance_port()
    except Exception:                                    # noqa: BLE001
        port = None
    if port is None:
        return None
    p = {"instructions": system, "messages": [{"role": "user", "text": text}], "think": False,
         "temperature": temperature, "presence_penalty": 0.0, "max_tokens": max_tokens, "_slot": PROMPT_SLOT}
    # 11-20: right after a start the brain answers "loading" for up to a minute; wait for it rather than give up
    # (that made a song instrumental).
    until = time.monotonic() + 90
    while True:
        out: list[str] = []
        try:
            _chat_llama(p, lambda o: out.append(o.get("v", "")) if o.get("t") == "text" else None, cancel, port)
            break
        except Exception:                                # noqa: BLE001
            if cancel.is_set() or time.monotonic() > until:
                return None
            time.sleep(3)
    raw = "".join(out).strip()
    a, b = raw.find("{"), raw.rfind("}")
    try:
        # strict=False: the model writes real line breaks inside the lyrics string. Strict parsing refused those and
        # every refused answer became an instrumental (the owner, 11-20: "a lot of lyric-less songs").
        d = json.loads(raw[a:b + 1], strict=False) if a >= 0 and b > a else None
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _write_song(idea: str, lyrics: str, instrumental: bool, seconds: int | None, safe: bool | str, cancel,
                style: str = "", language: str = "en", album: str = "", artists: str = "") -> dict | None:
    """The chat model's take on the idea as {title, tags, lyrics, bpm, key, seconds}; None when it can't."""
    text = idea[:4000] + (("\n\nMy lyrics:\n" + lyrics[:6000]) if lyrics and not instrumental else "")
    system = _song_system(seconds, instrumental, bool(lyrics), safe, style, language, album, artists)
    d = None
    for _ in range(2):                                 # once more when the answer is unusable or has no lyrics
        d = _llm_json(system, text, cancel)
        if cancel.is_set():
            return None
        if (d is not None and str(d.get("tags") or "").strip()
                and (instrumental or lyrics or len(str(d.get("lyrics") or "").strip()) > 20)):
            break
        d = None
    if d is None:
        return None
    try:
        bpm = max(60, min(180, int(d.get("bpm"))))
    except (TypeError, ValueError):
        bpm = None
    key = str(d.get("key") or "").strip()
    tags = str(d["tags"]).strip()[:600]
    voice = " ".join(str(d.get("voice") or "").split())[:240] if not instrumental else ""
    if voice:
        # 11-20 (the owner: "close but not voice wise"): the voice goes first, where the model weighs it most
        tags = f"{voice}, {tags}"
    if not instrumental and not any(w in tags.lower() for w in ("vocal", "voice", "choir", "singer", "rap")):
        tags += ", vocals"                             # ACE-Step leaves the singing out when no voice is named
    return {"title": str(d.get("title") or "").strip()[:80], "tags": tags,
            "lyrics": str(d.get("lyrics") or "").strip()[:8000], "bpm": bpm,
            "key": key if key in SONG_KEYS else None, "seconds": _song_seconds(d.get("seconds"))}


def _song_inputs(p: dict) -> dict:
    idea = str(p.get("prompt") or "").strip()
    lyrics = str(p.get("lyrics") or "").strip()
    instrumental = p.get("instrumental") is True or lyrics == "[Instrumental]"
    lang = p.get("language") if p.get("language") in SONG_LANGS else "en"
    return {"idea": idea, "lyrics": "" if instrumental else lyrics, "instrumental": instrumental,
            "style": str(p.get("tags") or "").strip()[:600], "language": lang,
            "seconds": _song_seconds(p.get("seconds")), "safe": p.get("safe") is True,
            "artists": _artists_text(p.get("artists")), "rating": _lyric_rating(p)}


def job_song_write(p: dict, emit, cancel):
    """11-20: "write the lyrics first": the writer's plan only (title, style, lyrics, length), for the owner to read
    and change before anything is sung. Uses no graphics card."""
    s = _song_inputs(p)
    if not s["idea"] and not s["lyrics"]:
        raise UserFacing("Say what the song should be.")
    emit({"t": "status", "v": "Writing the song…"})
    w = _write_song(s["idea"] or "a song with these lyrics", s["lyrics"], s["instrumental"], s["seconds"], s["rating"],
                    cancel, s["style"], s["language"], artists=s["artists"])
    if cancel.is_set():
        return
    if w is None:
        raise UserFacing("The chat model couldn't write it just now. Try again in a moment.")
    if s["lyrics"]:
        w["lyrics"] = s["lyrics"]
    if s["instrumental"]:
        w["lyrics"] = "[Instrumental]"
    emit({"t": "song_plan", **w})


def _sing(tags: str, lyrics: str, seconds: int, seed: int, bpm: int, key: str, language: str, emit, cancel,
          label: str = "Making the song…", reference: str | None = None, keep: float = 0.45,
          match: str = "start", revoice: float = 0.6) -> bytes:
    return asyncio.run(_comfy_run(_song_workflow(tags, lyrics, seconds, seed, bpm, key, language, reference, keep,
                                                 match, revoice),
                                  emit, cancel, audio=True, label=label))


SONG_REF_MAX = 30 * 1024 * 1024                         # a reference recording: 30 MB at most
SONG_REF_EXT = {"mp3", "m4a", "wav", "aac", "flac", "ogg", "caf", "aiff", "aif", "mp4"}


_KEY_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]   # Krumhansl-Kessler profiles
_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def _analyse_audio(path: Path, seconds: float = 90.0) -> tuple[int | None, str | None]:
    """11-20: a recording's tempo (BPM) and key ("A minor"), so a song made from it keeps them. Plain numpy on the
    first 90 s: tempo from the autocorrelation of the onset strength, key from a chroma profile matched against
    the major and minor key profiles. (None, None) when it can't be read."""
    try:
        import av
        import numpy as np
    except ImportError:
        return None, None
    sr = 22050
    try:
        with av.open(str(path)) as c:
            rs = av.AudioResampler(format="s16", layout="mono", rate=sr)
            chunks, total = [], 0
            for frame in c.decode(audio=0):
                for f in rs.resample(frame):
                    a = f.to_ndarray().reshape(-1)
                    chunks.append(a)
                    total += a.size
                if total >= sr * seconds:
                    break
        y = np.concatenate(chunks).astype(np.float32) / 32768.0 if chunks else None
    except Exception as e:                             # noqa: BLE001
        log.info("audio analysis skipped: %s", e.__class__.__name__)
        return None, None
    if y is None or y.size < sr * 8:
        return None, None
    n, hop = 2048, 512
    frames = 1 + (y.size - n) // hop
    idx = np.arange(n)[None, :] + hop * np.arange(frames)[:, None]
    spec = np.abs(np.fft.rfft(y[idx] * np.hanning(n), axis=1))
    # tempo: onset strength (positive spectral flux), its autocorrelation, the strongest lag in 60-180 BPM
    flux = np.maximum(0.0, np.diff(np.log1p(spec), axis=0)).sum(axis=1)
    flux -= flux.mean()
    ac = np.correlate(flux, flux, mode="full")[flux.size - 1:]
    fps = sr / hop
    lags = np.arange(ac.size)
    ok = (lags >= fps * 60 / 180) & (lags <= fps * 60 / 60)
    bpm = None
    if ok.any():
        best = lags[ok][np.argmax(ac[ok])]
        bpm = int(round(60 * fps / best))
    # key: energy per pitch class from 55 Hz to 2 kHz, matched against every major and minor key
    freqs = np.fft.rfftfreq(n, 1 / sr)
    band = (freqs >= 55) & (freqs <= 2000)
    pcs = (np.round(12 * np.log2(freqs[band] / 440.0)) + 9).astype(int) % 12
    energy = spec[:, band].sum(axis=0)
    chroma = np.bincount(pcs, weights=energy, minlength=12)
    if chroma.sum() <= 0:
        return bpm, None
    best_key, best_r = None, -2.0
    for mode, prof in (("major", _MAJOR), ("minor", _MINOR)):
        for k in range(12):
            r = np.corrcoef(chroma, np.roll(prof, k))[0, 1]
            if r > best_r:
                best_key, best_r = f"{_KEY_NAMES[k]} {mode}", r
    return bpm, best_key if best_key in SONG_KEYS else None


def _song_reference(p: dict, followed: str | None = None) -> tuple[str, Path] | None:
    """11-20: the owner's own recording or one of their songs ("reference", base64), written to ComfyUI's private
    input folder for this one job (job_song deletes it). None when there is none. `followed`: see _song_reference_url."""
    url = str(p.get("reference_url") or "").strip()
    if url:
        return _song_reference_url(url, p, followed)
    b64 = p.get("reference")
    if not isinstance(b64, str) or not b64:
        return None
    try:
        data = base64.b64decode(b64, validate=False)
    except (ValueError, TypeError):
        raise UserFacing("That recording couldn't be read.")
    if not data or len(data) > SONG_REF_MAX:
        raise UserFacing("That recording is too large (30 MB at most).")
    ext = str(p.get("reference_ext") or "m4a").lower().strip(".")
    ext = ext if ext in SONG_REF_EXT else "m4a"
    import uuid
    folder = COMFY_PRIVATE / "input"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"songref_{uuid.uuid4().hex}.{ext}"
    (folder / name).write_bytes(data)
    return name, folder / name


SONG_URL_MAX_S = 15 * 60                                # a linked recording: 15 minutes at most


def _song_check_url(url: str) -> str:
    """11-22: the cheap part of a link, no download: its scheme, then every redirect hop and what it resolves to must
    be public. Returns the URL after its redirects. job_song runs it BEFORE the (slow) picture-engine start."""
    import urllib.parse
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise UserFacing("That link isn't a web address.")
    return _song_follow(url)


def _song_reference_url(url: str, p: dict, followed: str | None = None) -> tuple[str, Path]:
    """11-20 (the owner: "I have stuff on YouTube ... of my old recordings, don't make me download those"): the
    audio of a web link (YouTube and the other sites yt-dlp knows), fetched for this one job into ComfyUI's private
    input folder and deleted after it, like a sent recording. Its length is put in p["reference_seconds"].
    `followed`: the URL job_song already checked with _song_check_url (a value from this process, never from the phone);
    without it the link is checked here."""
    import urllib.parse
    import uuid
    url = followed or _song_check_url(url)             # 11-22: every hop (and what it resolves to) must be public
    try:
        import yt_dlp
    except ImportError:
        raise UserFacing("Your computer needs yt-dlp for links (it's in requirements.txt).")
    folder = COMFY_PRIVATE / "input"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"songref_{uuid.uuid4().hex}"

    def short(info, *, incomplete):
        d = info.get("duration")
        return "too long" if d and d > SONG_URL_MAX_S else None
    opts = {"format": "bestaudio[ext=m4a]/bestaudio/best", "outtmpl": str(folder / (stem + ".%(ext)s")),
            "quiet": True, "no_warnings": True, "noplaylist": True, "max_filesize": SONG_REF_MAX,
            "match_filter": short, "noprogress": True, "cachedir": False, "socket_timeout": 20, "retries": 2,
            "proxy": ""}                               # "" = direct (yt-dlp's no-proxy setting; not tried here)
    # 11-22: no external downloader is asked for here. The real check is _SongNetGuard below (connect level, see
    # _song_follow for what it does not cover); _CheckedDL is the friendly first line.
    class _CheckedDL(yt_dlp.YoutubeDL):
        def urlopen(self, req):                        # every request yt-dlp itself makes: its host must be public
            _song_host_public(urllib.parse.urlsplit(getattr(req, "url", None) or str(req)).hostname)
            res = super().urlopen(req)
            final = getattr(res, "url", None)          # a redirect yt-dlp followed itself: refuse to read from a private end
            try:
                if final:
                    _song_host_public(urllib.parse.urlsplit(final).hostname)
            except UserFacing:
                try:
                    res.close()
                except Exception:                      # noqa: BLE001
                    pass
                raise
            return res
    try:
        with _SongNetGuard():                          # extract AND download: no connection to a private address
            with _CheckedDL(opts) as y:
                info = y.extract_info(url, download=True)
    except Exception as e:                             # noqa: BLE001
        for f in folder.glob(stem + ".*"):
            f.unlink(missing_ok=True)
        log.info("song link failed: %s", e.__class__.__name__)
        raise UserFacing("Couldn't get the audio from that link. Check it opens, is public or unlisted, and is "
                         "under 15 minutes.")
    files = sorted(folder.glob(stem + ".*"))
    if not files:
        raise UserFacing("That link has no audio Fixer can use (or it's over 15 minutes).")
    for extra in files[1:]:
        extra.unlink(missing_ok=True)
    if isinstance(info, dict) and info.get("duration"):
        p["reference_seconds"] = int(info["duration"])
    return files[0].name, files[0]


def _is_private_host(host: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _ip_not_public(ip) -> bool:
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped                            # ::ffff:127.0.0.1 is 127.0.0.1
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
            or ip.is_unspecified or not ip.is_global)  # not is_global also covers 100.64.0.0/10 (Tailscale, CGNAT)


def _song_host_public(host: str | None) -> None:
    """11-22: refuse a link host that is, or RESOLVES to, a private / loopback / link-local / reserved address. Numeric
    forms (2130706433, 127.1, 0x7f000001) are read the way the network stack reads them, and a name is resolved, so
    127.0.0.1.nip.io or a LAN hostname is caught too. A name that does not resolve at all is let through: yt-dlp then
    fails on its own with the plain 'couldn't get the audio' message."""
    import ipaddress
    import socket
    h = (host or "").strip().strip("[]").rstrip(".").lower()
    if not h:
        raise UserFacing("That link isn't a web address.")
    inside = UserFacing("That link points inside your own network.")
    if h == "localhost" or h.endswith((".localhost", ".local", ".lan", ".internal")):
        raise inside
    try:
        if _ip_not_public(ipaddress.ip_address(socket.inet_aton(h))):
            raise inside
    except (OSError, ValueError):
        pass                                           # not a numeric IPv4 form
    try:
        infos = socket.getaddrinfo(h, None)
    except (OSError, ValueError):                      # ValueError: a malformed name (empty or over-long label)
        return
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            continue
        if _ip_not_public(ip):
            raise inside


import socket as _socket_for_guard
# 11-22: .depth > 0 in a thread = a song-link fetch is running there. Kept on the socket module, beside the wrappers
# that read it, so a second copy of this module (run as __main__ and imported) shares the one the wrappers use.
_SONG_NET = getattr(_socket_for_guard, "_fixer_song_net", None) or threading.local()
_socket_for_guard._fixer_song_net = _SONG_NET


class _SongNetGuard:
    """Context manager: while it is open, THIS thread may not connect a socket to a private, loopback, link-local,
    reserved, multicast or unspecified address (see _install_song_net_guard). Nests; cleared on the way out."""

    def __enter__(self):
        _SONG_NET.depth = getattr(_SONG_NET, "depth", 0) + 1
        return self

    def __exit__(self, *exc):
        _SONG_NET.depth = max(0, getattr(_SONG_NET, "depth", 1) - 1)
        return False


class _PrivatePeer(OSError):
    """The guard's refusal (an OSError, so every caller's own error handling already copes with it)."""


def _checked_peer(family: int, address):
    """The address to connect to, or OSError when it is not a public one. A literal is checked as it stands (IPv6
    scope ids cut off, IPv4-mapped IPv6 read as the IPv4 inside). A NAME (connect accepts them) is resolved here, once,
    and the checked answer is what gets connected to, so DNS cannot change between the check and the connect."""
    import errno
    import ipaddress
    import socket
    if family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple) or len(address) < 2:
        return address                                 # AF_UNIX and the like: not a link's business
    host = address[0]
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("latin-1")
    host = str(host)
    refused = _PrivatePeer(errno.EACCES, "A song link may not connect to an address inside your own network.")
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        if host in ("", "<broadcast>"):                # INADDR_ANY / broadcast
            raise refused
        infos = [i for i in socket.getaddrinfo(host, address[1], family, socket.SOCK_STREAM) if i[0] == family]
        if not infos:
            raise OSError(errno.EADDRNOTAVAIL, "That link's host has no usable address.")
        address = infos[0][4]
        ip = ipaddress.ip_address(str(address[0]).split("%")[0])
    if _ip_not_public(ip):
        raise refused
    return address


def _install_song_net_guard() -> None:
    """11-22 (finding 25/79): thin wrappers around socket.socket.connect and connect_ex, installed once at import.
    ONLY in a thread with a _SongNetGuard open do they refuse a private peer address (raise OSError); in every other
    thread (the asyncio loop, ComfyUI and llama traffic, localhost) they pass straight through. This is the real
    check: it sees the address actually connected to, after DNS and after any redirect, whoever follows it. ssl's
    SSLSocket.connect / connect_ex reach socket.socket.connect through super() (Lib/ssl.py, _real_connect), so https
    is covered too."""
    import socket
    if getattr(socket.socket.connect, "_fixer_song_guard", False):
        return
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self, address):
        if getattr(_SONG_NET, "depth", 0):
            address = _checked_peer(self.family, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if getattr(_SONG_NET, "depth", 0):
            try:
                address = _checked_peer(self.family, address)
            except _PrivatePeer as e:
                return e.errno                         # connect_ex reports a refusal as an error number, never raises it
        return real_connect_ex(self, address)
    connect._fixer_song_guard = connect_ex._fixer_song_guard = True
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex


_install_song_net_guard()


def _song_follow(url: str, max_redirects: int = 5) -> str:
    """11-22: the URL after its redirects, followed by hand so each hop is checked (scheme, then host via
    _song_host_public) BEFORE anything is fetched from it. yt-dlp is then given the last hop. The walk uses a GET (its
    body is never read), since a server can answer HEAD and GET differently. A server that errors just ends the walk:
    that URL goes to yt-dlp as it is. No proxy from the environment is used (it would hide the real peer).
    The host checks are the friendly first line (they say why). The strict line is _SongNetGuard, held here and around
    the whole yt-dlp run: a connection to a private address is refused at connect time, so a redirect yt-dlp follows
    itself is never sent and DNS that changes between the check and the connect cannot get through.
    NOT covered: an external downloader subprocess (ffmpeg, which yt-dlp can pick by itself for some HLS streams;
    this code passes no external-downloader option, so it never asks for one), a request handler that does not use
    Python sockets (curl_cffi), and a thread yt-dlp starts itself (the guard is per thread; these options start none)."""
    import urllib.error
    import urllib.parse
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):            # a 3xx surfaces as HTTPError below instead of being followed
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    for _ in range(max_redirects + 1):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise UserFacing("That link isn't a web address.")
        _song_host_public(u.hostname)
        try:
            with _SongNetGuard():
                opener.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-0"}),
                            timeout=8).close()
            return url
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            code = e.code
            e.close()
            if code in (301, 302, 303, 307, 308) and loc:
                url = urllib.parse.urljoin(url, loc)
                continue
            return url
        except Exception as e:                         # noqa: BLE001
            if isinstance(e, _PrivatePeer) or isinstance(getattr(e, "reason", None), _PrivatePeer):
                raise UserFacing("That link points inside your own network.")   # it resolved inward after the check
            return url
    raise UserFacing("That link redirected too many times.")


def _song_card(status) -> None:
    if _worker_alive():
        WORKER.stop()                                  # the diffusers helper holds graphics memory too
    COMFY.ensure_current(status)
    _room_for_pictures()


def job_song(p: dict, emit, cancel):
    """A song from an idea ("prompt") and the owner's chosen sound ("tags"), with their lyrics, written ones or none
    ("instrumental"); 15-360 s or Auto. "write": false (Another take) sings the given tags and lyrics as they are."""
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    if not _song_ready():
        raise UserFacing("The song model isn't on your computer yet.")
    s = _song_inputs(p)
    if not s["idea"] and not s["lyrics"] and not s["style"]:
        raise UserFacing("Say what the song should be.")
    lyrics, seconds = s["lyrics"], s["seconds"]
    seed = int(p.get("seed") or int.from_bytes(os.urandom(4), "big")) % (2 ** 32)
    title = str(p.get("title") or "").strip()[:80]
    tags = s["style"] or s["idea"] or "pop song"
    try:
        bpm = max(60, min(180, int(p.get("bpm") or 0))) if p.get("bpm") else 120
    except (TypeError, ValueError):
        bpm = 120
    key = p.get("key") if p.get("key") in SONG_KEYS else "C major"
    as_is = p.get("write") is False and bool(s["style"]) and (bool(lyrics) or s["instrumental"])
    if not as_is:
        status("Writing the song…")
        w = _write_song(s["idea"] or "a song with these lyrics", lyrics, s["instrumental"], seconds, s["rating"],
                        cancel, s["style"], s["language"], artists=s["artists"])
        if cancel.is_set():
            return
        if w:
            title, tags = title or w["title"], w["tags"]
            bpm, key = w["bpm"] or bpm, w["key"] or key
            if not lyrics and not s["instrumental"]:
                lyrics = w["lyrics"]
            seconds = seconds or w["seconds"]
        elif not lyrics and not s["instrumental"]:
            emit({"t": "note", "v": "The chat model couldn't write lyrics just now, so this one is instrumental."})
    if s["instrumental"] or not lyrics:
        lyrics = "[Instrumental]"
    carded = False
    link = str(p.get("reference_url") or "").strip()
    followed = None
    if link:
        # 11-22: the cheap link check (scheme, redirects, where it resolves; nothing is downloaded) comes BEFORE the
        # engine start, which can take minutes: a bad link is refused at once
        status("Checking the link…")
        followed = _song_check_url(link)
        if cancel.is_set():
            return
    if p.get("reference_url") or p.get("reference"):
        # 11-22: ComfyUI starts BEFORE the recording is written into its private input folder (Comfy.ensure wipes that
        # folder when it starts the engine, which used to delete the file); the later _song_card is then a no-op
        _song_card(status)
        carded = True
        if cancel.is_set():
            return
    if p.get("reference_url"):
        status("Getting the recording from the link…")
    ref = _song_reference(p, followed)                 # a link sets p["reference_seconds"]
    try:
        if ref:
            # 11-20: the recording's own tempo and key, so the song made from it keeps them
            status("Listening to the recording…")
            r_bpm, r_key = _analyse_audio(ref[1])
            while r_bpm and r_bpm > 180:
                r_bpm //= 2
            while r_bpm and r_bpm < 60:
                r_bpm *= 2
            if r_bpm:
                bpm = r_bpm
            if r_key:
                key = r_key
        if not seconds:
            ref_s = _song_seconds(p.get("reference_seconds")) if ref else None
            seconds = ref_s or (SONG_AUTO_DEFAULT if lyrics == "[Instrumental]" else _lyrics_seconds(lyrics))
        ref_len = _song_seconds(p.get("reference_seconds")) if ref else None
        if ref_len and seconds > ref_len:
            seconds = ref_len                          # the song starts from the recording: no longer than it
        if cancel.is_set():
            return
        if not carded:
            _song_card(status)
        _timing(model="ACE-Step 1.5", steps=8, count=1)
        status("Loading the song model…")
        mp3 = _sing(tags, lyrics, seconds, seed, bpm, key, s["language"], emit, cancel,
                    reference=ref[0] if ref else None,
                    keep=_keep_of(p),
                    match=p.get("reference_match") if p.get("reference_match") in ("start", "voice", "both", "revoice")
                    else "start", revoice=float(p.get("revoice") or 0.6))
    finally:
        if ref:
            ref[1].unlink(missing_ok=True)             # the recording never outlives the job
    if cancel.is_set() or not mp3:
        return
    art_id = secrets.token_hex(6) if p.get("artwork") is True else None
    emit({"t": "progress", "v": 1.0})
    emit({"t": "audio", "v": base64.b64encode(mp3).decode(), "format": "mp3", "seconds": seconds, "seed": seed,
          "title": title, "tags": tags, "lyrics": lyrics, "bpm": bpm, "key": key, "language": s["language"],
          **({"art_id": art_id} if art_id else {})})
    if art_id and not cancel.is_set():                 # 11-20: artwork after the song, so the song comes first
        status("Painting the artwork…")
        png = _album_cover(_art_prompt(title, tags, lyrics, s["idea"], s["safe"], cancel), seed, cancel)
        if png and not cancel.is_set():
            emit({"t": "cover", "v": base64.b64encode(png).decode(), "art_id": art_id})
        elif not cancel.is_set():
            emit({"t": "note", "v": "The song is done; its artwork couldn't be painted this time."})


def _art_prompt(title: str, tags: str, lyrics: str, idea: str, safe: bool, cancel, wish: str = "") -> str:
    """One vivid sentence for a song's cover picture, from the song (and the owner's own wish, when given)."""
    sys_ = ("You design cover art for songs. Answer with one JSON object and nothing else: "
            '{"cover": one vivid sentence describing a striking square cover picture: subject, setting, colours, '
            "light and style, matching the song's mood and genre, with no words, letters or logos in it}."
            + (" The owner described what they want; follow it closely." if wish else "")
            + (" Keep it clean: nothing sexual or explicit." if safe else ""))
    text = (f"Owner's wish: {wish[:600]}\n" if wish else "") + (
        f"Song: {title[:80]}\nIdea: {idea[:600]}\nSound: {tags[:400]}\nLyrics:\n{lyrics[:1500]}")
    d = _llm_json(sys_, text, cancel, max_tokens=300, temperature=0.8)
    cover = str((d or {}).get("cover") or "").strip()[:500]
    return cover or wish or f"{title}, {tags}".strip(", ")[:500] or "abstract colourful music artwork"


def job_artwork(p: dict, emit, cancel):
    """11-20 (the owner: "add artwork after or during the creation of the song or album"): a new square picture for
    a song or an album ("art_id" is the phone's key, sent back with it). "prompt" is the owner's own description;
    without one it is designed from the song's title, sound and lyrics."""
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    art_id = str(p.get("art_id") or "").strip()[:64]
    if not art_id:
        raise UserFacing("Which song is that for?")
    wish = str(p.get("prompt") or "").strip()
    status("Designing the artwork…")
    prompt = _art_prompt(str(p.get("title") or ""), str(p.get("tags") or ""), str(p.get("lyrics") or ""),
                         str(p.get("idea") or ""), p.get("safe") is True, cancel, wish)
    if cancel.is_set():
        return
    _song_card(status)
    status("Painting the artwork…")
    seed = int.from_bytes(os.urandom(4), "big")
    png = _album_cover(prompt, seed, cancel)
    if cancel.is_set():
        return
    if not png:
        try:
            have_model = _cover_model() is not None
        except Exception:                              # noqa: BLE001
            have_model = False
        if have_model:                                 # 11-22: the model is there; the painting itself failed
            raise UserFacing("The picture engine couldn't paint the artwork this time. Try again in a moment.")
        raise UserFacing("No everyday picture model is on your computer to paint it.")
    emit({"t": "progress", "v": 1.0})
    emit({"t": "cover", "v": base64.b64encode(png).decode(), "art_id": art_id, "prompt": prompt})


def _album_system(n: int, instrumental: bool, safe: bool, style: str, language: str, artists: str = "") -> str:
    lang = SONG_LANGS.get(language, "English")
    return ("You plan albums for a music model. From the owner's theme, answer with one JSON object and nothing else: "
            '{"title": the album title, '
            '"sound": 10-20 comma-separated style tags every track shares: genre, era, mood, instruments, vocal type '
            '(or "instrumental") and production, '
            '"cover": a vivid one-sentence description of the album cover picture, with no words or letters in it, '
            f'"tracks": exactly {n} objects in running order, each {{"title": the track title, "idea": one or two '
            'sentences: what this track is about and how it differs (tempo, energy, mood) from its neighbours}}}. '
            "Give the album a shape: a strong opener, variety in the middle, a closer that lands."
            + (f" The owner chose this sound; build the shared tags from it and keep all of it: {style[:600]}." if style else "")
            + (" It is an instrumental album." if instrumental else "")
            + _inspired(artists).replace("in the tags", 'in "sound"').replace(", title or lyrics", ", the titles or the cover")
            + (f" Titles in {lang}." if lang != "English" and not instrumental else "")
            + (" Keep everything clean: nothing sexual or explicit." if safe else ""))


def _cover_model():
    """The everyday picture model ready for a cover, or None when there is none."""
    a = _auto_models()
    return _first_ready([x for x in a.get("picture_safe") or [] if x not in _adult_models()])


def _album_cover(prompt: str, seed: int, cancel) -> bytes | None:
    """A square cover from the everyday picture model; None when there is none or it fails (the songs still come)."""
    first = _cover_model()
    if first is None or not prompt:
        return None
    try:
        v = {**first["defaults"], "prompt": prompt + ", album cover art, no text, no letters", "detail": "Off"}
        png = asyncio.run(_comfy_run(_recipe_workflow(first, v, 1024, 1024, seed), lambda o: None, cancel))
    except Exception as e:                             # noqa: BLE001
        log.info("album cover skipped: %s", e.__class__.__name__)
        return None
    return png or None


def job_album(p: dict, emit, cancel):
    """11-20 (the owner: "full albums"): a theme -> the chat model plans a tracklist with one shared sound and a cover;
    then each track is written and sung in turn, sent as it finishes, so the first ones play while the rest are made.
    Stop ends it at once; the tracks already sent are kept."""
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    if not _song_ready():
        raise UserFacing("The song model isn't on your computer yet.")
    s = _song_inputs(p)
    if not s["idea"] and not s["style"]:
        raise UserFacing("Say what the album should be.")
    try:
        n = max(ALBUM_TRACKS[0], min(ALBUM_TRACKS[1], int(p.get("tracks") or 6)))
    except (TypeError, ValueError):
        n = 6
    base_seed = int(p.get("seed") or int.from_bytes(os.urandom(4), "big")) % (2 ** 32)
    status("Planning the album…")
    plan = _llm_json(_album_system(n, s["instrumental"], s["rating"] == "clean" or s["safe"], s["style"], s["language"],
                                   s["artists"]),
                     s["idea"] or "an album in this sound", cancel, max_tokens=2200)
    if cancel.is_set():
        return
    tracks = [t for t in (plan or {}).get("tracks") or [] if isinstance(t, dict) and t.get("title")][:n]
    if not plan or not tracks:
        raise UserFacing("The chat model couldn't plan the album just now. Try again in a moment.")
    album_id = secrets.token_hex(6)
    title = str(plan.get("title") or "Album").strip()[:80]
    sound = str(plan.get("sound") or s["style"] or s["idea"]).strip()[:600]
    head = {"album_id": album_id, "album_title": title, "tracks": len(tracks)}
    emit({"t": "album", **head, "sound": sound,
          "titles": [str(t.get("title")).strip()[:80] for t in tracks]})
    _song_card(status)
    status("Painting the cover…")
    cover = _album_cover(str(plan.get("cover") or "").strip()[:500], base_seed, cancel)
    if cancel.is_set():
        return
    if cover:
        emit({"t": "cover", "v": base64.b64encode(cover).decode(), **head})
    _timing(model="ACE-Step 1.5", steps=8, count=len(tracks))
    made = 0
    for i, t in enumerate(tracks):
        if cancel.is_set():
            break
        k = len(tracks)
        tt = str(t.get("title")).strip()[:80]
        status(f"Writing track {i + 1} of {k}…")
        idea = (f'Track {i + 1} of {k} of the album "{title}" ({s["idea"]}). This track: "{tt}": '
                f'{str(t.get("idea") or "").strip()[:600]}')
        w = _write_song(idea, "", s["instrumental"], s["seconds"], s["rating"], cancel, s["style"], s["language"],
                        album=sound, artists=s["artists"])
        if cancel.is_set():
            break
        tags = w["tags"] if w else sound
        lyrics = "[Instrumental]" if s["instrumental"] or not w or not w["lyrics"] else w["lyrics"]
        bpm, key = (w["bpm"] or 120, w["key"] or "C major") if w else (120, "C major")
        seconds = s["seconds"] or (w or {}).get("seconds") or (
            SONG_AUTO_DEFAULT if lyrics == "[Instrumental]" else _lyrics_seconds(lyrics))
        seed = _part_seed(base_seed, i + 1)

        def part(o, _i=i, _k=k):
            if o.get("t") == "progress":
                emit({"t": "progress", "v": round(min(0.99, (_i + o["v"]) / _k), 3)})
            elif o.get("t") != "status":
                emit(o)
        mp3 = _sing(tags, lyrics, seconds, seed, bpm, key, s["language"], part, cancel,
                    label=f"Making track {i + 1} of {k}…")
        if cancel.is_set() or not mp3:
            break
        made += 1
        emit({"t": "audio", "v": base64.b64encode(mp3).decode(), "format": "mp3", "seconds": seconds, "seed": seed,
              "title": tt, "tags": tags, "lyrics": lyrics, "bpm": bpm, "key": key, "language": s["language"],
              "track": i + 1, **head})
    if made:
        emit({"t": "progress", "v": 1.0})


def _photo_size(img, megapixels: float = 1.0) -> tuple[int, int]:
    """The photo's own shape at about `megapixels`, sides divisible by 16."""
    w, h = img.size
    scale = (megapixels * 1_000_000 / max(1, w * h)) ** 0.5
    return max(256, int(w * scale) // 16 * 16), max(256, int(h * scale) // 16 * 16)


def _strip_prompt_weighting(text: str) -> str:
    r"""Remove ComfyUI emphasis/attention weighting while keeping the words: '(phrase:1.3)' -> 'phrase' (any float
    weight), '(phrase)' / '((phrase))' emphasis -> 'phrase', '[phrase]' de-emphasis -> 'phrase'. Escaped '\(' '\)'
    (and '\[' '\]') survive as the literal characters. Char-scan, no dependencies, robust to multiple and nested
    groups. Used only for the Qwen text encoders, which ignore this syntax and would otherwise read the parentheses
    and numbers as part of the prompt; CLIPTextEncode (Chroma/SDXL) honours it, so those are left alone."""
    if not text:
        return text
    s = str(text)
    out: list[str] = []
    opens: list[int] = []                      # index in `out` where each still-open ( or [ group's content begins
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in "()[]":
            out.append(s[i + 1])               # an escaped bracket is a literal character, not a group delimiter
            i += 2
            continue
        if c in "([":
            opens.append(len(out))             # drop the delimiter itself; remember where its content starts
        elif c == ")":
            if opens:
                start = opens.pop()
                tail = "".join(out[start:])
                m = re.search(r":\s*[0-9]+(?:\.[0-9]+)?\s*$", tail)   # a trailing (phrase:1.3) weight, if any
                if m:
                    del out[start + m.start():]
            else:
                out.append(c)                  # an unmatched ) stays literal
        elif c == "]":
            if opens:
                opens.pop()                    # [de-emphasis]: drop the brackets, keep the words
            else:
                out.append(c)
        else:
            out.append(c)
        i += 1
    r = "".join(out)
    r = re.sub(r"[ \t]+", " ", r)               # collapse the doubled spaces a removed group can leave
    r = re.sub(r"\s+,", ",", r)                 # no space before a comma
    r = re.sub(r",\s*(?=,)", "", r)             # collapse commas doubled by an emptied group
    r = re.sub(r",(?=\S)", ", ", r)             # one space after each comma
    return r.strip(" \t\n,")                    # no dangling commas or space at the ends


def _model_prompt_grammar(builder: str, prompt: str, negative: str | None) -> tuple[str, str | None]:
    """The single place per-model prompt grammar lives. Today it only strips the ComfyUI weighting the Qwen text
    encoders ignore (qwen21 text-to-image, and the Qwen edit encoders); SDXL/Chroma use CLIPTextEncode, which honours
    the weighting, so they pass through untouched. Future per-model phrasing (tag-vs-prose rewriting, etc.) belongs
    here too, keyed off `builder`."""
    if str(builder or "").startswith("qwen"):
        prompt = _strip_prompt_weighting(prompt)
        if negative:
            negative = _strip_prompt_weighting(negative)
    return prompt, negative


def _recipe_workflow(r: dict, v: dict, w: int, h: int, seed: int, loras: list | None = None) -> dict:
    """The ComfyUI workflow for recipe `r` with values `v` (the recipe's defaults filled in by the caller)."""
    prompt = v.get("prompt") or "high quality photo"
    builder = r.get("builder", "")
    # Builder identity and the finished prompt (after _apply_toggles and the LoRA-trigger appends upstream) meet here,
    # just before the per-builder workflow is built, so this is where per-model grammar is applied.
    prompt, negative = _model_prompt_grammar(builder, prompt, v.get("negative"))
    if builder == "qwen21":
        return _qwen21_workflow(prompt, w, h, seed, v.get("cfg", 1.0), negative or "",
                                _qwen21_explicit(v.get("explicit"), v.get("brain_explicit"), v.get("base_recipe")),
                                loras)
    if builder == "chroma":
        return _chroma_workflow(prompt, negative or "", w, h, seed, v["steps"], v["cfg"], v["sampler"],
                                v["scheduler"], v["shift"], loras)
    if builder == "sdxl":
        return _sdxl_workflow(prompt, negative or r.get("default_negative", ""), w, h, seed, v["steps"],
                              v["cfg"], v["sampler"], v["scheduler"], loras, ckpt=r["ckpt"])
    raise UserFacing("That picture model isn't available.")


def _chat_recipe(model: str) -> dict | None:
    """The Create recipe behind a phone picture-model id, if its files are on this computer. A retired id
    (an old phone still asking for Z-Image) resolves to the model that replaced it."""
    model = RETIRED_PICTURE_IDS.get(model or "", model or "")
    r = CREATE_RECIPES.get(COMFY_RECIPES.get(model, ""))
    return r if r and _recipe_ready(r) else None


def _recipe_ready(r: dict) -> bool:
    """All repos there, and the recipe's own model file (when it names one) actually downloaded."""
    if not all(_snapshot(n) is not None for n in r["needs"]):
        return False
    if "ready" in r and not r["ready"]():
        return False
    if "file" in r:
        snap = _snapshot(r["file"][0])
        return snap is not None and (snap / r["file"][1]).is_file()
    return True


def _comfy_options(node: str, field: str) -> list[str]:
    try:
        req = COMFY._get(f"/object_info/{node}")[node]["input"]["required"][field]
        return list(req[0]) if isinstance(req[0], list) else []
    except Exception:
        return []


# Sampler / schedule menus in plain words: only the few that suit each model, the recommended one first and marked.
# The phone sends the whole label back; _menu_value() turns it back into ComfyUI's name.
SAMPLER_WORDS = {
    "res_multistep": "clean and fast",
    "euler": "clean and predictable, the safe choice",
    "euler_ancestral": "looser, more variety, less exact",
    "dpmpp_2m": "sharp and detailed, a great all-rounder",
    "dpmpp_2m_sde": "more fine texture and grain, a little random",
    "dpmpp_sde": "rich detail, slower",
    "uni_pc": "fast, good with few steps",
}
SCHEDULE_WORDS = {
    "simple": "even spacing",
    "beta": "extra care on fine detail at the end",
    "karras": "the classic SDXL choice, crisp",
    "exponential": "softer, smoother",
    "sgm_uniform": "smooth and balanced",
    "normal": "standard spacing",
}
RECIPE_CHOICES = {   # builder -> (samplers, schedules), recommended first
    "chroma": (["euler", "res_multistep", "dpmpp_2m", "uni_pc"], ["beta", "simple", "sgm_uniform"]),
    "qwen21": (["euler"], ["simple"]),
    "sdxl": (["dpmpp_2m_sde", "dpmpp_2m", "euler_ancestral", "dpmpp_sde"], ["karras", "exponential", "normal"]),
}


def _labelled(names: list[str], words: dict, default: str) -> list[str]:
    return [f"{n} ({'recommended: ' if n == default else ''}{words.get(n, '')})" for n in names]


def _menu_value(choice) -> str:
    """'dpmpp_2m (sharp and detailed...)' -> 'dpmpp_2m' (old builds send the bare name, which passes through)."""
    return str(choice or "").split(" (", 1)[0].strip()


def _recipe_controls(r: dict) -> list[dict]:
    d = r["defaults"]
    samplers, schedules = RECIPE_CHOICES.get(r["builder"], (["euler"], ["simple"]))
    c = [
        {"key": "prompt", "label": "Describe the picture", "kind": "text",
         "help": r.get("prompt_help", "Say what's in it, where, the light and the style. Your words go to the model "
                                      "exactly as written.")},
        {"key": "shape", "label": "Shape", "kind": "shape", "options": list(SHAPES), "default": "square",
         "help": "Square, tall (phone screen) or wide (TV). Changes the layout, not the quality."},
        {"key": "count", "label": "How many", "kind": "int", "min": 1, "max": 4, "step": 1, "default": 1,
         "help": "Pictures from one press, each a little different. Each one adds the same time again."},
        {"key": "cfg", "label": "Guidance", "kind": "float", "min": 1.0, "max": 10.0, "step": 0.1, "default": d["cfg"],
         "left": "Looser, more creative", "right": "Stricter, follows your words",
         "help": r.get("cfg_help", "How strictly to follow your words.")},
        {"key": "steps", "label": "Steps", "kind": "int", "min": 1, "max": 50, "step": 1, "default": d["steps"],
         "advanced": True, "left": "Faster", "right": "More detail",
         "help": f"How many passes the model makes. Recommended: {d['steps']}. Fewer is quicker but rougher; more "
                 "takes longer and past the recommended number rarely looks better."},
        {"key": "sampler", "label": "Sampler (how it draws)", "kind": "menu",
         "options": _labelled(samplers, SAMPLER_WORDS, d["sampler"]),
         "default": _labelled([d["sampler"]], SAMPLER_WORDS, d["sampler"])[0], "advanced": True,
         "help": "Changes the look slightly: cleaner vs more texture. The recommended one is right for this model."},
        {"key": "scheduler", "label": "Schedule (where effort goes)", "kind": "menu",
         "options": _labelled(schedules, SCHEDULE_WORDS, d["scheduler"]),
         "default": _labelled([d["scheduler"]], SCHEDULE_WORDS, d["scheduler"])[0], "advanced": True,
         "help": "Small effect. Leave on the recommended one unless a picture looks too soft or too harsh."},
        {"key": "shift", "label": "Shift", "kind": "float", "min": 1.0, "max": 10.0, "step": 0.5, "default": d["shift"],
         "advanced": True, "left": "Finer detail", "right": "Bolder layout",
         "help": "Where the model spends its effort: low favours small detail, high favours the big shapes and "
                 "composition. Leave on the recommended value unless faces or layouts look off."},
        {"key": "seed", "label": "Seed", "kind": "seed", "advanced": True,
         "help": "The starting point of a picture. Random gives a new picture each time; the same seed with the same "
                 "settings gives the same picture again, so you can change one thing and compare."},
    ]
    if r.get("builder") in ("chroma", "sdxl", "qwen21"):   # 11-21: the Realism slider (candid <-> studio), main setting
        c.insert(next(i for i, x in enumerate(c) if x["key"] == "cfg") + 1,
                 {"key": "realism", "label": "Realism", "kind": "float", "min": 0.0, "max": 1.0, "step": 0.1,
                  "default": 0.0, "left": "Studio-clean", "right": "Candid, real",
                  "help": "Slide right for phone-snapshot looks: real skin, flash, uneven light, not a studio shot. "
                          "0 leaves your picture exactly as it was."})
    if r.get("builder") == "qwen21" and "qwen21" in _adult_models():   # 11-19: an everyday model has no add-on menu
        lora = _qwen21_lora()
        ctl = {"key": "explicit", "label": "Explicit add-on", "kind": "menu", "options": ["Auto", "On", "Off"],
               "default": "Auto",
               "help": "On loads the explicit add-on for nudity and sex. Off never does. Auto: only when you change a "
                       "picture that was made with it on. Your words are never changed."}
        if lora is None:
            ctl.update({"options": ["Off"], "default": "Off",
                        "help": "The explicit add-on isn't on your computer yet."})
        c.insert(next(i for i, x in enumerate(c) if x["key"] == "count") + 1, ctl)
    if r.get("negative"):
        c.insert(1, {"key": "negative", "label": "Leave out", "kind": "text", "advanced": True,
                     "help": "Things to keep out of the picture (e.g. 'text, blurry, extra fingers'). Empty uses the "
                             "model's own recommended list."})
    if not r.get("uses_shift", True):
        c = [x for x in c if x["key"] != "shift"]
    if not r.get("uses_scheduler", True):
        c = [x for x in c if x["key"] != "scheduler"]
    if not r.get("uses_steps", True):
        c = [x for x in c if x["key"] != "steps"]
    if not r.get("uses_sampler", True):
        c = [x for x in c if x["key"] != "sampler"]
    return c


PROMPT_TOGGLES = ROOT / "prompt_toggles.json"     # the Create screen's prompt helpers; read fresh every time
PHONE_CONFIG = ROOT / "phone_config.json"         # texts / word lists the phone uses (no app update needed)


def _phone_config_for_send() -> dict:
    try:
        cfg = json.loads(PHONE_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    if SEARCH_URL:
        cfg["search_url"] = SEARCH_URL                 # the phone's own search when the computer is off
    return cfg


def job_config(p: dict, emit, cancel):
    """The phone's tunable texts and lists (it keeps the last copy for when the computer is off)."""
    emit({"t": "info", "config": _phone_config_for_send()})


def _prompt_style(r: dict) -> str:
    """Which phrasing a recipe's toggles use: Chroma / SDXL sentences with negatives, or the positive-only sentences
    (the legacy "zimage" style) that Qwen-Image 2.1 uses at guidance 1, where negatives do nothing."""
    return {"chroma": "chroma", "sdxl": "sdxl"}.get(r.get("builder", ""), "zimage")


def _toggle_phrase(it: dict, style: str) -> dict:
    """One toggle's wording for a style. 11-22: SDXL models get their own short tag wording ("sdxl" in
    prompt_toggles.json); a toggle that has none falls back to the Chroma sentences it used before."""
    phr = it.get(style)
    if style == "sdxl" and not isinstance(phr, dict):
        phr = it.get("chroma")
    return phr if isinstance(phr, dict) else {}


def _toggle_library() -> list[dict]:
    try:
        return json.loads(PROMPT_TOGGLES.read_text(encoding="utf-8")).get("groups", [])
    except Exception:
        return []


# 11-19: style tips that are 18+ although their words don't say so outright (the explicit-words check catches the
# rest). Hidden on the phone and never applied while 18+ is off. phone_config "adult_toggles" adds more ids; an item
# can also carry "explicit": true / false in prompt_toggles.json (false wins over every guess).
ADULT_TOGGLES_DEFAULT = ("e_seductive", "e_pleasure", "p_fours", "p_spread", "w_lingerie", "w_shirt", "w_stockings")


def _toggle_is_adult(it: dict) -> bool:
    if isinstance(it.get("explicit"), bool):
        return it["explicit"]
    try:
        extra = json.loads(PHONE_CONFIG.read_text(encoding="utf-8")).get("adult_toggles") or []
    except Exception:
        extra = []
    if it.get("id") in ADULT_TOGGLES_DEFAULT or it.get("id") in extra:
        return True
    words = " ".join([str(it.get("label") or "")]
                     + [str((it.get(s) or {}).get("p") or "") for s in ("chroma", "sdxl", "zimage")])
    return _words_explicit(words)


def _recipe_toggles(r: dict) -> list[dict]:
    """The toggle groups for one recipe: label + the phrasing for its model (only items that have one)."""
    style = _prompt_style(r)
    out = []
    for g in _toggle_library():
        items = [{"id": it["id"], "label": it["label"], "explicit": _toggle_is_adult(it)} for it in g.get("items", [])
                 if _toggle_phrase(it, style).get("p") or _toggle_phrase(it, style).get("n")]
        if items:
            out.append({"name": g.get("name", ""), "items": items})
    return out


def _apply_toggles(r: dict, v: dict) -> dict:
    """Adds the chosen toggles to the prompt (and their negatives to 'Leave out' where the model uses one)."""
    ids = set(v.get("toggles") or [])
    if not ids:
        return v
    style = _prompt_style(r)
    pos, neg = [], []
    safe = getattr(_TL, "safe", False)                   # 11-19: 18+ off -> no 18+ style tips
    for g in _toggle_library():
        for it in g.get("items", []):
            if it.get("id") in ids and not (safe and _toggle_is_adult(it)):
                phr = _toggle_phrase(it, style)
                if phr.get("p"):
                    pos.append(phr["p"])
                if phr.get("n"):
                    neg.append(phr["n"])
    sep = ", " if style == "sdxl" else ". "              # tag wording joins with commas, sentences with full stops
    prompt = (v.get("prompt") or "").strip().rstrip(".")
    v = {**v, "prompt": sep.join([x for x in [prompt] + pos if x])}
    if neg and r.get("negative"):
        base = (v.get("negative") or "").strip()
        v["negative"] = ", ".join([base or r.get("default_negative", CHROMA_NEGATIVE)] + neg)
    return v


# ----- Realism (11-21) -------------------------------------------------------------------------------------------
# A single "Realism" slider (0..1) nudges a picture from studio-clean toward a candid phone snapshot. It is a built-in
# MACRO (see the roles & macros store): candid phrasing is added to the prompt, anti-studio terms to the negative, CFG
# is lowered, and a LoRA assigned to the "realism" role rides along through _job_loras (so 18+ filtering and the cap
# still apply). 0, or any non-photo model, leaves the picture exactly as it was. Tiers are cumulative. The owner can
# edit every number/phrase by saving a macro id "realism" (merged key-by-key over this default) or deleting it to reset.
REALISM_DEFAULT = {
    "id": "realism", "name": "Realism (candid <-> studio)", "builtin": True,
    "lora_role": "realism", "lora_scale": 1.0, "cfg_drop": 0.8, "cfg_floor": 1.5,
    "prompt": [
        [0.15, {"p": "Phone snapshot. Uneven natural light",
                "z": "A phone snapshot in uneven natural light"}],
        [0.45, {"p": "On-camera flash. Visible film grain. Real skin texture with pores",
                "z": "on-camera flash, visible grain, real skin texture with pores"}],
        [0.75, {"p": "Slightly off-centre, unposed, cluttered background. Not a studio shot",
                "z": "slightly off-centre, unposed, cluttered everyday background, not a studio shot"}],
    ],
    "negative": [
        [0.15, "studio lighting, softbox, backdrop"],
        [0.45, "professional photography, retouched, airbrushed, flawless skin, perfect lighting"],
        [0.75, "bokeh, cinematic, glossy, magazine, dslr, 85mm portrait"],
    ],
}


def _realism_macro() -> dict:
    """REALISM_DEFAULT with any owner edits (a macro id 'realism' in macros.json) merged over it, key by key."""
    try:
        for m in (_macros().get("macros") or []):
            if m.get("id") == "realism":
                return {**REALISM_DEFAULT, **{k: val for k, val in m.items() if val is not None}}
    except Exception:                                    # noqa: BLE001
        pass
    return dict(REALISM_DEFAULT)


def _apply_realism(r: dict, v: dict) -> dict:
    """Fold the Realism slider (v['realism'], 0..1) into prompt / negative / cfg / loras. No-op at <0.05 or on a
    non-photo model, so a picture is only ever touched when the owner asks for it."""
    try:
        t = float(v.get("realism") or 0)
    except (TypeError, ValueError):
        t = 0.0
    builder = r.get("builder", "")
    if t < 0.05 or builder not in ("chroma", "sdxl", "qwen21"):
        return v
    macro = _realism_macro()
    key = "z" if _prompt_style(r) == "zimage" else "p"   # qwen21 uses positive-only phrasing at guidance 1
    v = dict(v)
    have = (v.get("prompt") or "").lower()
    adds = []
    for thr, phr in (macro.get("prompt") or []):
        if t + 1e-9 >= float(thr):
            text = (phr or {}).get(key) or ""
            if text and text.lower() not in have:
                adds.append(text)
                have += " " + text.lower()
    if adds:
        base = (v.get("prompt") or "").strip().rstrip(".")
        v["prompt"] = ". ".join([x for x in [base] + adds if x])
    if builder in ("chroma", "sdxl"):                    # qwen21 at cfg 1.0: negatives do nothing, cfg must stay
        if r.get("negative"):
            negs = [n for thr, n in (macro.get("negative") or []) if t + 1e-9 >= float(thr) and n]
            if negs:
                base = (v.get("negative") or "").strip() or r.get("default_negative", CHROMA_NEGATIVE)
                v["negative"] = ", ".join([base] + negs)
        try:
            cfg = float(v.get("cfg", r["defaults"].get("cfg", 3.5)))
            v["cfg"] = max(float(macro.get("cfg_floor", 1.5)), cfg - float(macro.get("cfg_drop", 0.8)) * t)
        except (TypeError, ValueError, KeyError):
            pass
    rl = _role_lora(str(macro.get("lora_role", "realism")))   # None until the owner assigns a realism LoRA in Settings
    if rl:
        file, strength = rl
        if not any(x.get("name") == file for x in (v.get("loras") or [])):
            scaled = round(strength * float(macro.get("lora_scale", 1.0)) * t, 2)
            v["loras"] = [{"name": file, "strength": scaled}] + list(v.get("loras") or [])   # first so the cap keeps it
    return v


def job_comfy_recipes(p: dict, emit, cancel):
    """The Create screen's list: recipes whose files are on this computer, their controls, and the LoRA files.
    11-19: answers at once. It used to start ComfyUI first (COMFY.ensure(), a 25-110 s cold start) before sending a
    list it already had, so opening Make sat on "Asking your computer…" for up to two minutes. The engine now starts
    in the background, warming for the first picture while the owner fills in the form."""
    if not COMFY.running():
        threading.Thread(target=_quietly, args=(COMFY.ensure,), daemon=True, name="comfy-start").start()
    adult = _adult_models()
    recipes = [{"id": k, "name": r["name"], "kind": r["kind"], "note": r.get("note"),
                "lora_family": r.get("lora_family"), "controls": _recipe_controls(r),
                "toggles": _recipe_toggles(r), "adult": k in adult}          # 11-19: an 18+ model
               for k, r in CREATE_RECIPES.items() if _recipe_ready(r)]
    loras = sorted(f.name for f in COMFY_LORAS.glob("*.safetensors")) if COMFY_LORAS.is_dir() else []
    threading.Thread(target=_lora_nsfw_backfill, daemon=True, name="lora-nsfw").start()   # 11-19: Make opened
    emit({"t": "info", "recipes": recipes, "loras": loras, "lora_library": _lora_entries()})


# ---------------------------------------------------------------- Make's style recipes (11-17)


# The recipes and quick picks live in a PRIVATE file beside the server (never in git; the owner edits it, and an
# edit shows the next time Make opens). The public example below is the fallback when it is missing.
RECIPE_FILE = ROOT / "recipes.json"
RECIPE_EXAMPLE = Path(__file__).with_name("recipes.example.json")


def _shape_name(size) -> str | None:
    """'832x1216' -> the closest of SHAPES by aspect ratio."""
    try:
        w, h = (int(x) for x in str(size).lower().split("x"))
    except (TypeError, ValueError):
        return None
    ratio = w / max(1, h)
    return min(SHAPES, key=lambda k: abs(ratio - SHAPES[k][0] / SHAPES[k][1]))


# 11-21: quick-pick priority — the order the models want (front = most attention). Scene-defining first (who,
# framing, act, setting), cosmetic last (hair). Turn off with auto_order_picks=0 in local.json.
PICK_PRIORITY = ("subject", "camera", "act", "setting", "pose", "anatomy_f", "anatomy_m", "body_size", "body",
                 "fitness", "height", "undress", "outfit", "age", "heritage", "expression", "fluids", "reactions",
                 "kink", "fetish", "features", "lighting", "accessories", "props", "hair", "hair_color")


def _quickselect_groups() -> list:
    src = RECIPE_FILE if RECIPE_FILE.is_file() else RECIPE_EXAMPLE
    try:
        data = json.loads(src.read_text(encoding="utf-8-sig"))
        return (data.get("quickselect") or {}).get("groups") or []
    except Exception:                                    # noqa: BLE001
        return []


def _order_picks(prompt: str) -> str:
    """Reorder the recognised quick-pick phrases in a prompt by category priority (scene-defining first, cosmetic
    last). Free-typed words keep their place at the front; a pick's weight wrapper (phrase:1.2) is preserved. Off
    when auto_order_picks is 0 in local.json."""
    if not prompt or not int(_tune("auto_order_picks", 1)):
        return prompt
    opt2pri = {}
    for g in _quickselect_groups():
        gid = g.get("id") or ""
        pri = PICK_PRIORITY.index(gid) if gid in PICK_PRIORITY else len(PICK_PRIORITY)
        for o in (g.get("options") or []) + (g.get("explicit_options") or []):
            opt2pri[str(o).strip().lower()] = pri
    if not opt2pri:
        return prompt
    free, picks = [], []
    for seg in prompt.split(","):
        s = seg.strip()
        if not s:
            continue
        core = re.sub(r"^\((.*):[0-9.]+\)$", r"\1", s).strip().lower()
        if core in opt2pri:
            picks.append((opt2pri[core], s))
        else:
            free.append(s)
    if not picks:
        return prompt
    picks.sort(key=lambda x: x[0])
    return ", ".join(free + [s for _, s in picks])


def _make_catalog() -> dict:
    """recipes.json in the shape the phone's Make screen reads (recipes for models that aren't here are left out)."""
    src = RECIPE_FILE if RECIPE_FILE.is_file() else RECIPE_EXAMPLE
    try:
        data = json.loads(src.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    recipes = []
    for d in data.get("recipes") or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        kind = "video" if d.get("kind") == "video" else "picture"
        # 11-19: a style is 18+ because of what it is (the "Explicit" group, or a video with the explicit add-on), never
        # because of the model it names. (Marking every Chroma / Juggernaut style 18+ put a dozen everyday photo
        # styles in the 18+ row, ahead of the real ones, and hid them with 18+ off; the owner: "a bunch of styles
        # rolled into 18+".) An everyday style that names an 18+ model runs on the everyday model instead, below.
        item = {"id": str(d["id"]), "name": d.get("name") or d["id"], "group": d.get("group") or "", "kind": kind,
                "explicit": d.get("group") == "Explicit" or (kind == "video" and bool(d.get("explicit_addon"))),
                "prefix": d.get("prompt_prefix") or "", "suffix": d.get("prompt_suffix") or "",
                "chips": d.get("chips") or [], "note": d.get("note"), "shape": _shape_name(d.get("shape"))}
        if kind == "video":
            item.update({"model": "video", "numbers": {}, "menus": {},
                         "explicit_video": "on" if d.get("explicit_addon") else "off"})
        else:
            model_id = d.get("model") or ""
            if item["explicit"] != (model_id in _adult_models()):
                # 11-19: an 18+ recipe on an everyday model (always safe) moves to an 18+ model, and an everyday recipe
                # on an 18+ model moves to the everyday one, each with that model's own settings: the old model's
                # numbers and its add-on menu would mean nothing there.
                alt = _first_ready(_auto_models().get("picture_explicit" if item["explicit"] else "picture_safe") or [])
                if alt is None:
                    continue
                model_id = _recipe_id(alt)
                d = {k: x for k, x in d.items()
                     if k not in ("steps", "cfg", "shift", "sampler", "scheduler", "negative", "explicit_addon")}
            r = CREATE_RECIPES.get(model_id)
            if not r or not _recipe_ready(r):
                continue
            dflt = r["defaults"]
            nums = {}
            try:
                if d.get("steps") is not None and r.get("uses_steps", True):
                    nums["steps"] = float(d["steps"])
                if d.get("cfg") is not None:
                    nums["cfg"] = float(d["cfg"])
                if d.get("realism") is not None:
                    nums["realism"] = float(d["realism"])
                if d.get("shift") is not None and r.get("uses_shift", True):
                    nums["shift"] = float(d["shift"])
            except (TypeError, ValueError):             # one bad number in recipes.json must not take down every style
                log.warning("recipes.json: the style %r has a number that isn't a number; left out", item["id"])
                continue
            menus = {}
            if r.get("uses_sampler", True) and d.get("sampler") in SAMPLER_WORDS:
                menus["sampler"] = _labelled([d["sampler"]], SAMPLER_WORDS, dflt["sampler"])[0]
            if r.get("uses_scheduler", True) and d.get("scheduler") in SCHEDULE_WORDS:
                menus["scheduler"] = _labelled([d["scheduler"]], SCHEDULE_WORDS, dflt["scheduler"])[0]
            if "explicit" in dflt:
                menus["explicit"] = "On" if d.get("explicit_addon") else "Auto"
            item.update({"model": model_id, "numbers": nums, "menus": menus,
                         "negative": (d.get("negative") or None) if r.get("negative") else None})
        recipes.append(item)
    qs = data.get("quickselect") or {}
    return {"recipes": recipes, "quickselect": {"groups": qs.get("groups") or []},
            "lora_cap": int(_tune("lora_cap", 4))}


def job_make_recipes(p: dict, emit, cancel):
    """Make's recipe row and quick picks (op `make_recipes`). Read fresh on every call."""
    emit({"t": "info", **_make_catalog()})


def job_comfy_run(p: dict, emit, cancel):
    """One Create request: `count` pictures from a recipe and the phone's values, with live previews."""
    recipe = p.get("recipe") or ""
    if recipe in ("zimage", "zimage-nsfw"):            # an old phone's Make screen: use Juggernaut, with its settings
        recipe = "juggernaut"
        vals = {k: val for k, val in (p.get("values") or {}).items()
                if k not in ("steps", "cfg", "sampler", "scheduler", "shift", "detail")}
        p = {**p, "recipe": recipe, "values": vals}
    if p.get("safe") is True and recipe in _adult_models():
        # 11-19: 18+ off -> an 18+ model is quietly swapped for the everyday one (with that model's own settings).
        alt = _first_ready([x for x in _auto_models().get("picture_safe") or [] if x not in _adult_models()])
        if alt is None:
            raise UserFacing("No everyday picture model is on your computer, and 18+ is off.")
        recipe = _recipe_id(alt)
        vals = {k: val for k, val in (p.get("values") or {}).items()
                if k not in ("steps", "cfg", "sampler", "scheduler", "shift", "detail", "negative", "loras")}
        p = {**p, "recipe": recipe, "values": vals}
    r = CREATE_RECIPES.get(recipe)
    if not r:
        raise UserFacing("That recipe isn't on your computer.")
    v = {**r["defaults"], **(p.get("values") or {})}
    if v.get("image_mode") == "paint" and str(v.get("change") or "").strip():
        # Paint: the prompt describes what goes in the spot (the surroundings come from the picture itself); the
        # original prompt's words would fight a change like "smaller" or "red instead of blue".
        v["prompt"] = str(v["change"]).strip()
    v = _apply_toggles(r, v)
    v["sampler"], v["scheduler"] = _menu_value(v.get("sampler")), _menu_value(v.get("scheduler"))
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    v = _apply_realism(r, v)                            # 11-21: the Realism slider's candid push (no-op at 0)
    if float(v.get("realism") or 0) >= 0.05:
        status("Candid look on")
    v["prompt"] = _order_picks(v.get("prompt") or "")   # 11-21: quick-picks in category-priority order
    COMFY.ensure_current(status)                        # a freshly downloaded model, add-on, reader or LoRA shows up
    _room_for_pictures()
    if _worker_alive():
        WORKER.stop()
    shapes = {**SHAPES, **r.get("shapes", {})}          # a recipe may override a shape (SDXL draws wallpaper at ~1 MP)
    w, h = shapes.get(v.get("shape") or "square", shapes["square"])
    count = max(1, min(4, int(v.get("count") or 1)))
    seed = v.get("seed")
    seed = int(seed) if seed not in (None, "", -1) else int.from_bytes(os.urandom(4), "big")
    loras, triggers = _job_loras(v.get("loras"), r.get("lora_family"))
    if v.get("image_mode") not in ("edit", "reference"):   # an edit adds its editor's triggers in _create_pictures
        v["prompt"] = _with_triggers(v.get("prompt"), triggers)
    photo, photo_name, mode = None, None, str(v.get("image_mode") or "start")
    extra_names = []                                  # photos 2 and 3 for a multi-photo edit
    try:                                               # any failure before the drawing starts removes every photo written so far
        for b64 in (v.get("images") or [])[:2]:
            import uuid
            n = f"cr_{uuid.uuid4().hex}.png"
            extra_names.append(n)
            (COMFY_PRIVATE / "input").mkdir(parents=True, exist_ok=True)
            _image_in(b64).save(COMFY_PRIVATE / "input" / n, format="PNG")
        if v.get("image"):
            import uuid
            photo = _image_in(v["image"])
            photo_name = f"cr_{uuid.uuid4().hex}.png"
            (COMFY_PRIVATE / "input").mkdir(parents=True, exist_ok=True)
            photo.save(COMFY_PRIVATE / "input" / photo_name, format="PNG")
            if mode == "paint" and not v.get("mask"):
                raise UserFacing("Paint over the spot to change first.")
            if mode in ("edit", "reference") and not (_qwen_edit_ready() or _qwen_edit_ready(True) or _qwen21_ready()):
                raise UserFacing("Editing a photo needs a photo editor model on your computer.")
        if v.get("image_mode") == "paint" and photo is None:
            raise UserFacing("Paint to change needs the picture; try again from the viewer.")
    except BaseException:
        for n in ([photo_name] if photo_name else []) + extra_names:
            (COMFY_PRIVATE / "input" / n).unlink(missing_ok=True)
        raise
    try:
        _create_pictures(r, v, w, h, count, seed, loras, photo, photo_name, mode, emit, cancel, status,
                         extra_names, p.get("recipe"))
    finally:
        for n in ([photo_name] if photo_name else []) + extra_names:
            (COMFY_PRIVATE / "input" / n).unlink(missing_ok=True)


def _recipe_id(r: dict) -> str | None:
    return next((k for k, x in CREATE_RECIPES.items() if x is r), None)


def _has_addon(wf: dict) -> bool:
    """True when a 2.1 graph carries the explicit add-on (node "5" is only ever that)."""
    return (wf.get("5") or {}).get("class_type") == "LoraLoaderModelOnly"


def _create_pictures(r, v, w, h, count, seed, loras, photo, photo_name, mode, emit, cancel, status,
                     extra_names=(), recipe_id=None):
    recipe_id = recipe_id or _recipe_id(r)
    if recipe_id != COMFY.last_recipe or (photo_name and mode in ("edit", "reference")):   # 11-19: only when it
        status("Loading image model…")                                   # really loads (an edit uses the editor's)
    builder = r["builder"]
    own_explicit = recipe_id in QWEN_EXPLICIT_RECIPES
    if photo is not None and mode == "paint":
        _timing(model=r["name"], recipe=recipe_id, explicit=own_explicit, mp=None, steps=r["defaults"].get("steps"))
        _paint_picture(r, v, seed, loras, photo, v["mask"], emit, cancel)
        return
    prompt = v.get("prompt") or "high quality photo"
    # 11-22: an "edit" used to send only the bare instruction to a literal instruction-editor, which then ignored the
    # original make's requirements (the owner: "doesn't stick to the original make pic's requirements… lacks
    # understanding"). The phone now sends the make's own words as base_prompt; fold them in as the scene to preserve,
    # then state the change, so the editor edits a picture it understands instead of guessing from a one-line delta.
    base_prompt = str(v.get("base_prompt") or "").strip()
    if base_prompt and str(v.get("image_mode")) == "edit":
        instr = str(v.get("prompt") or "").strip()
        prompt = f"{base_prompt}. Change: {instr}." if instr else base_prompt
    editor = str(v.get("editor") or "auto")             # Make's Editor menu (11-17): auto, qwen, qwen-explicit, qwen21
    if editor == "flux":
        editor = "auto"                                  # a phone build from before 11-17: FLUX klein is gone
    for i in range(count):
        s_i = (seed + i) % (2 ** 32)
        editing = bool(photo_name and mode in ("edit", "reference"))
        # Same model for making and editing: the 2.1 editor is picked by choice, or by "Auto" for a picture that was
        # made with 2.1 (so make -> edit never reloads). Every other Auto edit stays 2511 / Rapid-AIO.
        use21 = editing and (editor == "qwen21" or (editor == "auto" and recipe_id == "qwen21"))
        if use21:
            if not _qwen21_ready():
                raise UserFacing("Qwen-Image 2.1 isn't all on your computer yet, so nothing was changed.")
            eloras, etrig = _job_loras(v.get("loras"), "qwen21")
            lora = _qwen21_explicit(v.get("explicit"), None, recipe_id)
            eprompt, _ = _model_prompt_grammar("qwen21", _with_triggers(prompt, etrig), None)
            wf = _qwen21_edit_workflow(eprompt, [photo_name, *extra_names], s_i, lora,
                                       loras=eloras, reference_size=(w, h) if mode == "reference" else None)
            budget = int(_tune("qwen21_edit_resolution", 1024))
            _timing(model=QWEN21_NAME, recipe=recipe_id, editor="qwen21", explicit=bool(lora), steps=8, count=count,
                    mp=(w * h / 1e6) if mode == "reference" else (None if budget else
                                                                  photo.size[0] * photo.size[1] / 1e6))
        elif editing:
            if editor == "qwen-explicit" and _qwen_edit_ready(True):
                explicit = True
            elif editor == "qwen":
                explicit = False
            else:
                explicit = _qwen_explicit(prompt, recipe_id)
            if not (explicit or _qwen_edit_ready()):
                raise UserFacing("Editing a photo needs a photo editor model on your computer.")
            qw, qh = _photo_size(photo) if mode == "edit" else (w, h)
            eloras, etrig = _job_loras(v.get("loras"), "qwen-edit", allow_explicit=explicit)   # plain editor: never 18+
            eprompt, _ = _model_prompt_grammar("qwen-edit", _with_triggers(prompt, etrig), None)
            wf = _qwen_edit_workflow(eprompt, [photo_name, *extra_names], s_i, qw, qh,
                                     explicit, loras=eloras)
            _timing(model="Qwen Rapid-AIO" if explicit else "Qwen Edit 2511", recipe=recipe_id,
                    editor="qwen-explicit" if explicit else "qwen", explicit=explicit, mp=qw * qh / 1e6, steps=4,
                    count=count)
        elif photo_name:
            pw, ph = _photo_size(photo)
            if builder == "qwen21":
                qprompt, qneg = _model_prompt_grammar("qwen21", prompt, v.get("negative") or "")
                wf = _qwen21_workflow(qprompt, pw, ph, s_i, v.get("cfg", 1.0), qneg or "",
                                      _qwen21_explicit(v.get("explicit"), None, recipe_id), loras,
                                      start=(photo_name, float(v.get("strength") or 0.6), pw, ph))
            else:
                wf = _recipe_workflow(r, v, pw, ph, s_i, loras)
                _start_from(wf, builder, photo_name, pw, ph, float(v.get("strength") or 0.6))
            _timing(model=r["name"], recipe=recipe_id, explicit=_has_addon(wf) or own_explicit, mp=pw * ph / 1e6,
                    steps=v.get("steps"), count=count)
        else:
            if builder == "qwen21":
                v = {**v, "base_recipe": recipe_id}
            wf = _recipe_workflow(r, v, w, h, s_i, loras)
            _timing(model=r["name"], recipe=recipe_id, explicit=_has_addon(wf) or own_explicit, mp=w * h / 1e6,
                    steps=v.get("steps"), count=count)
        part = lambda obj, i=i: emit({**obj, "v": round((i + obj["v"]) / count, 3)} if obj.get("t") == "progress"
                                     else obj)  # noqa: E731
        png = asyncio.run(_comfy_run(wf, part, cancel, previews=True))
        if cancel.is_set() or not png:
            return
        COMFY.last_recipe = None if editing else recipe_id   # an edit ran the editor's models, not the recipe's
        emit({"t": "image", "v": base64.b64encode(png).decode(), "seed": s_i, "index": i,
              "recipe": recipe_id})                      # so a picture the phone adopts later can still be changed
    emit({"t": "progress", "v": 1.0})


async def _comfy_run(workflow: dict, emit, cancel, previews: bool = False, frames: bool = False,
                     label: str = "Creating image…", audio: bool = False):
    """Queues one workflow, relays progress (and live previews when asked), returns the PNG bytes sent over the
    websocket. audio=True (songs): the saved audio file's bytes instead, read back and deleted from the output folder."""
    import uuid
    import aiohttp
    cid = uuid.uuid4().hex
    png = None
    pngs: list[bytes] = []
    last_preview = 0.0
    watch = _TimingWatch(workflow)                     # 11-17: seconds loading vs drawing, from ComfyUI's own events
    is21 = (workflow.get("4") or {}).get("class_type") == "QwenImage21Cache"
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"ws://127.0.0.1:{COMFY_PORT}/ws?clientId={cid}", max_msg_size=0) as ws:
            async with s.post(f"http://127.0.0.1:{COMFY_PORT}/prompt",
                              json={"prompt": workflow, "client_id": cid}) as r:
                body = await r.json(content_type=None)
                if r.status != 200 or "prompt_id" not in body:
                    raise UserFacing("The picture engine refused the job: " + str(body.get("error", body))[:200])
                pid = body["prompt_id"]
            started = False
            deadline = time.monotonic() + (2400 if frames else 900)   # a video's first load reads ~34 GB
            while True:
                if time.monotonic() > deadline:
                    await s.post(f"http://127.0.0.1:{COMFY_PORT}/interrupt")
                    raise UserFacing("That took too long on your computer and was stopped.")
                if cancel.is_set():
                    await s.post(f"http://127.0.0.1:{COMFY_PORT}/interrupt")
                    return b""
                try:
                    msg = await ws.receive(timeout=1)
                except asyncio.TimeoutError:
                    continue
                if msg.type == aiohttp.WSMsgType.BINARY:
                    data = msg.data
                    kind = struct.unpack(">I", data[4:8])[0] if len(data) > 8 else 0
                    if kind == 2:                                                  # PNG: the final picture
                        png = bytes(data[8:])
                        pngs.append(png)
                    elif kind == 1 and previews and time.monotonic() - last_preview > 0.4:   # JPEG: a live preview
                        last_preview = time.monotonic()
                        emit({"t": "preview", "v": base64.b64encode(bytes(data[8:])).decode()})
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    d = m.get("data", {})
                    if d.get("prompt_id") not in (None, pid):
                        continue
                    watch.on(m["type"], d)
                    if m["type"] == "progress":
                        if not started:
                            started = True
                            emit({"t": "status", "v": label})
                        emit({"t": "progress", "v": round(d["value"] / max(1, d["max"]), 3), "node": d.get("node")})
                    elif m["type"] == "execution_error":
                        if is21 and str(d.get("node_id")) == "2":
                            raise UserFacing("The Qwen-Image 2.1 text reader didn't load on your computer. "
                                             "Nothing was made.")
                        raise UserFacing("The picture engine failed: " + str(d.get("exception_message", ""))[:200])
                    elif m["type"] in ("execution_success",) or (m["type"] == "executing" and d.get("node") is None):
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise UserFacing("Lost the connection to the picture engine.")
        sound = b""
        if audio and not cancel.is_set():
            sound = await _comfy_audio_out(s, pid)
        try:
            await s.post(f"http://127.0.0.1:{COMFY_PORT}/history", json={"clear": True})   # keep nothing
        except Exception:
            pass
    if cancel.is_set():
        return [] if frames else b""
    watch.apply()
    if audio:
        if not sound:
            raise UserFacing("The song engine returned no song.")
        return sound
    if not png:
        raise UserFacing("The picture engine returned no picture.")
    return pngs if frames else png


async def _comfy_audio_out(s, pid: str) -> bytes:
    """The audio file a finished prompt saved: read through /view, then deleted (nothing is kept on the computer)."""
    async with s.get(f"http://127.0.0.1:{COMFY_PORT}/history/{pid}") as r:
        hist = await r.json(content_type=None)
    data = b""
    for out in ((hist.get(pid) or {}).get("outputs") or {}).values():
        for a in out.get("audio") or []:
            fn, sub = a.get("filename") or "", a.get("subfolder") or ""
            if not fn:
                continue
            async with s.get(f"http://127.0.0.1:{COMFY_PORT}/view",
                             params={"filename": fn, "subfolder": sub, "type": a.get("type") or "output"}) as v:
                if v.status == 200 and not data:
                    data = await v.read()
            base = (COMFY_PRIVATE / "output").resolve()
            f = (base / sub / fn).resolve()
            if f.is_relative_to(base):
                f.unlink(missing_ok=True)
    return data


def _quietly(fn):
    try:
        fn()
    except Exception:
        pass


def job_comfy_image(p: dict, emit, cancel):
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    r = _picture_recipe(p)
    if r is None:
        raise UserFacing("No everyday picture model is on your computer, and 18+ is off." if p.get("safe") is True
                         else "No picture model is on your computer.")
    COMFY.ensure_current(status)                        # a freshly downloaded model, add-on, reader or LoRA shows up
    _room_for_pictures()                               # dense brain: the card goes to the picture; the MoE brain stays
    if _worker_alive():
        WORKER.stop()                                  # the diffusers helper holds graphics memory too
    w, h = _fit(int(p.get("width", 1024)), int(p.get("height", 1024)), int(p.get("longest", 1024)))
    if r.get("builder") == "sdxl" and w * h > 1_100_000:   # SDXL is trained near 1 MP; a chat picture may ask for more
        sc = (1_100_000 / (w * h)) ** 0.5
        w, h = int(w * sc), int(h * sc)
    seed = int(p.get("seed") or int.from_bytes(os.urandom(4), "big")) % (2 ** 32)
    rid = _recipe_id(r)
    aloras, atrig = _job_loras(None, r.get("lora_family"))     # chat pictures: only the family's always-on LoRAs
    flag = p.get("explicit") if isinstance(p.get("explicit"), bool) else None     # the brain's own call (new phones)
    v = {**r["defaults"], "prompt": _with_triggers(p.get("prompt"), atrig), "detail": "Off", "brain_explicit": flag}
    wf = _recipe_workflow(r, v, w // 16 * 16, h // 16 * 16, seed, aloras)
    on = _has_addon(wf) or rid in QWEN_EXPLICIT_RECIPES
    _timing(model=r["name"], recipe=rid, explicit=on, mp=(w // 16 * 16) * (h // 16 * 16) / 1e6,
            steps=r["defaults"].get("steps"), count=1)
    if rid != COMFY.last_recipe:                       # 11-19: only when it really has to load something
        status("Loading image model…")
    png = asyncio.run(_comfy_run(wf, emit, cancel))
    if cancel.is_set() or not png:
        return
    COMFY.last_recipe = rid
    emit({"t": "progress", "v": 1.0})
    emit({"t": "image", "v": base64.b64encode(png).decode(), "seed": seed, "recipe": rid, "explicit": on})


def job_comfy_edit(p: dict, emit, cancel):
    """An instruction edit of a photo in ComfyUI: Qwen-Image 2.1 when the chat chose it, otherwise Qwen-Image-Edit
    2511 (or the Rapid-AIO merge for explicit edits). The phone's "remove" and painted edits crop a window around the
    marked area, send only that, and paste the result back softly, so this is a plain edit. The photo sits in
    ComfyUI's private input folder only for this job."""
    import uuid
    status = lambda s: emit({"t": "status", "v": s})  # noqa: E731
    want21 = p.get("model_choice") == "qwen21"
    if want21:
        COMFY.ensure_current(status)
    else:
        COMFY.ensure(status)
    _room_for_pictures()
    if _worker_alive():
        WORKER.stop()
    start = _image_in(p["image"])
    longest = int(p.get("longest", 1024))
    name = f"fx_{uuid.uuid4().hex}.png"
    path = COMFY_PRIVATE / "input" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    start.save(path, format="PNG")
    seed = int(p.get("seed") or int.from_bytes(os.urandom(4), "big")) % (2 ** 32)
    words = p.get("prompt") or "improve the photo"
    flag = p.get("explicit") if isinstance(p.get("explicit"), bool) else None     # the brain's call; old phones: words
    COMFY.last_recipe = None                           # 11-19: the editor's models replace the last recipe's
    try:
        status("Loading image model…")
        if want21:
            if not _qwen21_ready():
                raise UserFacing("Qwen-Image 2.1 isn't all on your computer yet, so nothing was changed.")
            aloras, atrig = _job_loras(None, "qwen21")
            lora = _qwen21_explicit(None, flag)
            wf = _qwen21_edit_workflow(_with_triggers(words, atrig), [name], seed, lora, loras=aloras)
            budget = int(_tune("qwen21_edit_resolution", 1024))
            _timing(model=QWEN21_NAME, recipe="qwen21", editor="qwen21", explicit=bool(lora), steps=8, count=1,
                    mp=None if budget else start.size[0] * start.size[1] / 1e6)
            explicit = bool(lora)
        else:
            explicit = (flag is True and _qwen_edit_ready(True)) or _qwen_explicit(words)
            if not (explicit or _qwen_edit_ready()):
                raise UserFacing("Editing a photo needs a photo editor model on your computer.")
            pw, ph = _photo_size(start, min(2.0, max(0.25, longest * longest / 1_000_000)))
            aloras, atrig = _job_loras(None, "qwen-edit", allow_explicit=explicit)            # plain editor: never 18+
            wf = _qwen_edit_workflow(_with_triggers(words, atrig), [name], seed, pw, ph, explicit, loras=aloras)
            _timing(model="Qwen Rapid-AIO" if explicit else "Qwen Edit 2511",
                    editor="qwen-explicit" if explicit else "qwen", explicit=explicit, mp=pw * ph / 1e6, steps=4,
                    count=1)
        png = asyncio.run(_comfy_run(wf, emit, cancel))
    finally:
        path.unlink(missing_ok=True)
    if cancel.is_set() or not png:
        return
    emit({"t": "progress", "v": 1.0})
    emit({"t": "image", "v": base64.b64encode(png).decode(), "seed": seed, "explicit": explicit})


def _comfy_edits_ready() -> bool:
    return COMFY.available() and (_qwen_edit_ready() or _qwen_edit_ready(True) or _qwen21_ready())


def _warmup_workflow(recipe: str) -> dict:
    """A 1-step 64x64 picture: makes ComfyUI read the recipe's models from disk into memory."""
    r = CREATE_RECIPES[recipe]
    if r.get("builder") == "qwen21":
        # No add-on; one short step (only the loaders matter: node "7" differs from a real job, the models do not).
        return _qwen21_workflow("warm-up", 64, 64, 0, sigmas="1.0, 0")
    return _recipe_workflow(r, {**r["defaults"], "prompt": "warm-up", "steps": 1, "detail": "Off"}, 64, 64, 0)


def job_comfy_warm(p: dict, emit, cancel):
    """The phone opened Fixer: get the chosen ComfyUI recipe's models into memory now, so the first picture doesn't
    wait for the disk. The chat model is left alone.
    11-19: this used to end with COMFY.free(), whose free_memory also emptied ComfyUI's RAM cache, so the warm-up
    threw away what it had just loaded. Now, beside the MoE brain (which ComfyUI starts around with --reserve-vram)
    the models simply stay put, as they do after a real picture; beside a dense brain they leave the card only."""
    model = RETIRED_PICTURE_IDS.get(p.get("model") or "", p.get("model") or "")
    recipe = COMFY_RECIPES.get(model)
    if p.get("safe") is True and recipe in _adult_models():
        recipe = None                                  # 11-19: 18+ off -> never preload an 18+ model
    if recipe and _chat_recipe(model) and recipe not in COMFY.warmed:
        COMFY.ensure_current()
        try:
            asyncio.run(_comfy_run(_warmup_workflow(recipe), lambda obj: None, cancel))
            done = not cancel.is_set()
        except Exception:
            done = False                               # a failed warm-up costs nothing: the picture loads as before
        if _is_moe(LAST_BRAIN):
            COMFY.last_recipe = recipe if done else COMFY.last_recipe
        else:
            COMFY.unload_card()
        if done:
            COMFY.warmed.add(recipe)
    emit({"t": "info", "warm": True})


def job_brain_warm(p: dict, emit, cancel):
    """The phone opened Fixer: start the chat model now (6-9 s from memory), so the first message doesn't wait.
    11-17: this used to warm the PICTURE model on every app open; ComfyUI's cold start and model load (25-110 s)
    then sat in front of the first chat. Pictures warm when the Make tab opens ("for": "make")."""
    repo = BRAIN_ALIASES.get(p.get("brain") or "", p.get("brain") or "") or LAST_BRAIN
    # 11-19: say at once whether the chat model is already up, before any load. A phone that hears anything but
    # "ready" answers on itself straight away instead of sitting behind this load (20-60 s cold), and switches back
    # to this computer when the closing frame below says "ready". Read without the lock: a load in progress holds it.
    choice = gguf_choice(repo)

    def brain_state() -> str:
        if choice is None:
            return "ready"                             # not a llama-server model: job_chat answers its own way
        s = LLAMA.state()
        if s == "ready" and LLAMA.repo != repo:
            return "off"                               # another model is up: this one still has to load
        return s

    # 11-19: app open is ONE request: this first frame also carries the phone's config (was a separate "config"
    # call) and how many finished results are held for this phone (its inbox fetches the line only when > 0).
    phone = p.get("_phone")
    held = sum(1 for j in QUEUE.held() if j.state == "ready" and not j.hidden and (phone is None or j.phone == phone))
    emit({"t": "info", "brain_state": brain_state(), "config": _phone_config_for_send(), "held": held})
    if choice is not None and not LLAMA.paused_for and (_is_moe(repo) or not VIDEO_BUSY.is_set()):
        try:
            LLAMA.ensure(repo, choice, lambda s: None)
        except Exception:
            pass                                       # a failed warm-up costs nothing: the chat loads as before
    emit({"t": "info", "warm": True, "brain_state": brain_state()})


def job_warm_skip(p: dict, emit, cancel):
    emit({"t": "info", "warm": False})


def job_warm(p: dict, emit, cancel):
    """(In the helper.) Loads the chosen diffusers picture model into RAM ahead of the first picture; it moves to
    the card only while drawing, so the chat model keeps its place."""
    repo = p.get("model")
    if _local_complete(repo):
        ENGINE.pipe(repo, "make", lambda s: None)      # only models already here: a warm-up never downloads
    emit({"t": "info", "warm": True})


def _schedule_uninstall(tmp_script: Path, task: str) -> None:
    """Runs the uninstaller as a one-off scheduled task: Task Scheduler starts it outside this server's job object,
    so it lives on when it stops the tray and this server, which it does first."""
    # A task's command is capped at 261 characters: a short launcher beside the copy carries the long arguments.
    launcher = tmp_script.with_name(f"fxu-{task[-8:]}.ps1")
    launcher.write_text(f"& '{tmp_script}' -HomeDir '{ROOT}' -Wait 5 -Task '{task}'\n"
                        f"Remove-Item -LiteralPath '{tmp_script}', $PSCommandPath -Force -ErrorAction SilentlyContinue\n",
                        encoding="utf-8")
    tr = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{launcher}"'
    for args in (["schtasks", "/Create", "/TN", task, "/TR", tr, "/SC", "ONCE", "/ST", "23:59", "/F"],
                 ["schtasks", "/Run", "/TN", task]):
        r = subprocess.run(args, capture_output=True, text=True, timeout=30, creationflags=0x08000000)
        if r.returncode != 0:
            raise UserFacing("Windows wouldn't start the uninstaller: " + (r.stderr or r.stdout).strip()[:200])


def job_uninstall(p: dict, emit, cancel):
    """11-20 (the owner: "a convenient way to delete both ends when we don't want it anymore"): the phone's "Remove
    Fixer everywhere". A clean wipe of this computer's side, nothing kept (uninstall.ps1): it answers first, then the
    uninstaller stops everything and deletes the install. Only with the phone's explicit confirmation word."""
    if p.get("confirm") != "remove-everything":
        raise UserFacing("Not confirmed.")
    src = ROOT / "uninstall.ps1"
    if not src.is_file():
        raise UserFacing("This computer's Fixer Desktop is too old to remove itself. Update it, or delete "
                         f"{ROOT} by hand.")
    tag = secrets.token_hex(4)
    tmp = Path(os.environ.get("TEMP") or tempfile.gettempdir()) / f"fixer-uninstall-{tag}.ps1"
    shutil.copyfile(src, tmp)
    _schedule_uninstall(tmp, f"FixerUninstall-{tag}")
    log.warning("uninstall requested by a paired phone; the uninstaller starts in 5 s")
    emit({"t": "info", "uninstalling": True, "home": str(ROOT)})


def job_forget(p: dict, emit, cancel):
    """The phone wiped its history: drop everything this PC holds in memory about it. The chat model keeps the last
    conversation in its prompt cache for as long as it runs, and the helper holds the last photo's job; stopping
    both releases it. Nothing was ever written to disk. Runs at once, even mid-job (ending that job is the point)."""
    LLAMA.stop()
    ENGINE.unload()                                    # also ends the helper process
    COMFY.stop()                                       # and ComfyUI, with its private folder; it restarts on next use
    TIMINGS.clear()                                    # the speed numbers hold no words, but "nothing is held" means nothing
    erased = ["the chat model's memory of your conversations",
              "the picture and video engine, with any photo, prompt or result it held",
              "its private temporary folder",
              "the line of pictures and videos, and any finished results it was holding"]
    # Then check, rather than assume: nothing running that could hold a conversation, the private folder gone.
    left = []
    if LLAMA.running():
        left.append("the chat model is still running")
    if _worker_alive():
        left.append("the picture helper is still running")
    if COMFY.running():
        left.append("the picture engine is still running")
    if COMFY_PRIVATE.exists() and any(COMFY_PRIVATE.rglob("*")):
        left.append("files in the private temporary folder")
    if QUEUE.has_work():                               # (QUEUE.clear() ran on the loop just before this)
        left.append("work still in the line")
    emit({"t": "info", "forgot": True, "erased": erased, "left": left})


# ---------------------------------------------------------------- LoRA library (add-ons)
# LoRAs live in COMFY_LORAS as "<clean-name>-<sha8>.safetensors"; their details in COMFY_LORAS/library.json. All of
# this runs on the PC; the phone only ever sees results. Nothing about a search (words, links, thumbnails) is written
# to disk, links are never logged, and the Civitai key is sent only to civitai.com / civitai.red.

LORA_UA = "Fixer/1.0"                                   # Cloudflare rejects Python's default agent
LORA_MAX_BYTES = 2 * 1024 ** 3
SAFE_HEADER_MAX = 16 * 1024 * 1024
LORA_LOCK = threading.Lock()                            # one download at a time, off the picture queue
CIVITAI_API = "https://civitai.com/api/v1"
CIVITAI_TOKEN = ROOT / "civitai_token"
LORA_FAMILIES = ("sdxl", "chroma", "qwen21", "qwen-edit", "wan22")
LORA_CIVITAI_BASE = {                                   # family -> Civitai baseModel labels (a superset per engine, so the
    "sdxl": ["SDXL 1.0", "Pony", "Illustrious", "NoobAI"],   # filter scopes a search without starving it of results)
    "chroma": ["Chroma"],
    "qwen21": ["Qwen", "Qwen 2.1"], "qwen-edit": ["Qwen", "Qwen 2.1"],   # Civitai labels Qwen LoRAs both ways
    "wan22": ["Wan Video 2.2 I2V-A14B", "Wan Video 2.2 T2V-A14B"],   # the 14B experts; the 5B model is not installed
}
LORA_WARN_RELATED = "Made for a related model, may look wrong."
LORA_NO_FIT = "This add-on doesn't fit any model on your computer."
LORA_WAN_5B = "This add-on was made for a smaller Wan video model that isn't on this computer."


def _civitai_token() -> str:
    try:
        return (sec.read_text(CIVITAI_TOKEN) or "").strip()   # sealed, or a legacy plaintext file
    except Exception:                                    # noqa: BLE001
        return ""


def _is_civitai_host(host: str) -> bool:
    h = (host or "").lower()
    return h in ("civitai.com", "civitai.red") or h.endswith(".civitai.com") or h.endswith(".civitai.red")


def _lora_headers(url: str) -> dict:
    import urllib.parse
    h = {"User-Agent": LORA_UA}
    tok = _civitai_token()
    if tok and _is_civitai_host(urllib.parse.urlsplit(url).hostname or ""):   # key only to Civitai, never a redirect host
        h["Authorization"] = f"Bearer {tok}"
    return h


def _public_host(host: str) -> None:
    """Refuse a host that resolves to a non-public address (SSRF guard)."""
    import ipaddress
    import socket
    if not host:
        raise UserFacing("That link has no address.")
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:                               # noqa: BLE001
        raise UserFacing("Couldn't resolve that address.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            raise UserFacing("That link points inside a private network and was refused.")


def _open_checked(url: str, timeout: float = 60, max_redirects: int = 6):
    """An https GET with every redirect re-checked: scheme https only, host must be public, Civitai key only on a
    Civitai host. Returns the response for the caller to read. Raises UserFacing on any refusal."""
    import urllib.error
    import urllib.parse
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):            # don't auto-follow: a 3xx surfaces as HTTPError below
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    seen = 0
    while True:
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https":
            raise UserFacing("Only secure (https) links are allowed.")
        _public_host(p.hostname or "")
        req = urllib.request.Request(url, headers=_lora_headers(url), method="GET")
        try:
            return opener.open(req, timeout=timeout)     # 2xx
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                e.close()
                if not loc or seen >= max_redirects:
                    raise UserFacing("That link redirected too many times.") from None
                url = urllib.parse.urljoin(url, loc)
                seen += 1
                continue
            e.close()
            if e.code in (401, 403) and _is_civitai_host(p.hostname or ""):
                raise UserFacing("A free Civitai key is needed for this one. Add one in Settings; it stays on your "
                                 "computer.") from None
            raise UserFacing(f"That link returned an error ({e.code}).") from None
        except (urllib.error.URLError, OSError) as e:
            raise UserFacing("Couldn't reach that link.") from e


def _api_json(url: str, timeout: float = 30):
    r = _open_checked(url, timeout=timeout)
    try:
        return json.loads(r.read(SAFE_HEADER_MAX) or b"null")
    finally:
        r.close()


def _api_text(url: str, timeout: float = 30, limit: int = 2_000_000) -> str:
    r = _open_checked(url, timeout=timeout)
    try:
        return r.read(limit).decode("utf-8", "replace")
    finally:
        r.close()


def _clean_lora_name(name: str) -> str:
    """A file-system-safe stem: no path separators, no leading dots, so a name can never leave the folder."""
    base = re.sub(r"\.safetensors$", "", (name or "").strip(), flags=re.I)
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._")
    return (base or "lora")[:60]


def _lora_path(fn) -> Path | None:
    """COMFY_LORAS / fn for a file name that came from the phone, or None when it is not a plain add-on file name:
    a bare '<name>.safetensors' (no folders, '..', drive, colon or absolute path) whose resolved parent is the add-on
    folder itself. Every place that joins a phone-supplied name to the folder goes through this."""
    if not isinstance(fn, str) or not fn or fn != fn.strip() or not fn.lower().endswith(".safetensors"):
        return None
    if any(c in fn for c in "/\\:\0") or ".." in fn or Path(fn).is_absolute() or Path(fn).name != fn:
        return None
    try:
        f = COMFY_LORAS / fn
        if f.resolve().parent != COMFY_LORAS.resolve():
            return None
    except (OSError, ValueError):
        return None
    return f


def _lora_lib() -> dict:
    try:
        d = json.loads(sec.read_text(COMFY_LORAS / "library.json") or "null")
        return d if isinstance(d, dict) else {}          # missing file / wrong shape: an empty library, never None
    except Exception:                                    # noqa: BLE001
        return {}


LIB_LOCK = threading.RLock()                            # 31: held for each library.json read-change-write, in every writer
                                                        # (jobs run on executor threads). Held only for that short step,
                                                        # never across a download, a ComfyUI stop or an await. Lock order:
                                                        # LORA_LOCK first, then this one.


def _lora_lib_for_update() -> dict:
    """The library for a read-change-write, to be called inside LIB_LOCK. Unlike _lora_lib it does not turn an
    unreadable file into {}: a file that exists but can't be unsealed or parsed is refused, so the change is not
    written over it as an empty library. A missing or empty file is a fresh library."""
    try:
        return sec.load_json_object(COMFY_LORAS / "library.json")
    except sec.ConfigUnreadable as e:
        log.warning("add-on list can't be read: %s", e)
        raise UserFacing("The list of your add-ons couldn't be read, so nothing was changed. On the PC, run "
                         "\"python pair.py --reset library\" in the Fixer folder: it renames the old list (kept, not "
                         "deleted) and starts an empty one, then add your add-ons again.") from e


def _lora_lib_write(lib: dict) -> None:
    _ensure_lora_store()
    sec.write_text(COMFY_LORAS / "library.json", json.dumps(lib))   # sealed at rest (DPAPI)


# ----- Roles & macros (11-21) ------------------------------------------------------------------------------------
# A phone-editable macros.json maps named ROLES (realism, skin-detail, undress-helper, qwen-explicit-addon, ...) to a
# LoRA file + strength, and user MACROS bundle prompt/negative/LoRAs/settings. A feature asks for a ROLE instead of
# naming a file, so the owner can reassign any add-on from Settings with no code change. Read fresh like library.json;
# a missing or corrupt file falls back to a built-in default so startup never breaks and nothing is ever auto-written.
MACROS_FILE = ROOT / "macros.json"
BUILTIN_ROLES = ("realism", "skin-detail", "undress-helper", "qwen-explicit-addon")


def _macros_defaults() -> dict:
    """What a fresh install sees before anything is saved: the explicit add-on already assigned to its live-tunable
    file/strength (so the Settings role editor shows it filled in), every other built-in role present but unset."""
    roles = {r: {"file": "", "strength": 1.0, "enabled": False} for r in BUILTIN_ROLES}
    qf = _tune_text("qwen21_explicit_lora", QWEN21_LORA_FILE)
    if qf and (qf in (_lora_lib() or {}) or (COMFY_LORAS / qf).is_file()):   # 11-21: only pre-fill when it's actually installed
        roles["qwen-explicit-addon"] = {"file": qf,
                                        "strength": max(0.0, min(2.0, _tune("qwen21_explicit_strength", 1.0))),
                                        "enabled": True}
    return {"roles": roles, "macros": []}


def _macros() -> dict:
    """macros.json as {roles, macros}, read fresh every use. Absent or corrupt -> built-in defaults (never raises,
    never writes). Missing sections are filled so callers index safely."""
    try:
        data = json.loads(sec.read_text(MACROS_FILE) or "null")
        if not isinstance(data, dict):
            raise ValueError
    except Exception:                                    # noqa: BLE001  (absent / corrupt -> defaults)
        return _macros_defaults()
    d = _macros_defaults()
    if isinstance(data.get("roles"), dict):
        d["roles"].update({k: v for k, v in data["roles"].items() if isinstance(v, dict)})
    if isinstance(data.get("macros"), list):
        d["macros"] = [m for m in data["macros"] if isinstance(m, dict)]
    return d


def _macros_write(data: dict) -> None:
    """Atomic write (tmp + replace), like _lora_lib_write."""
    sec.write_text(MACROS_FILE, json.dumps(data, indent=2, ensure_ascii=False))   # sealed at rest (DPAPI)


def _role_lora(name: str) -> tuple[str, float] | None:
    """(file, strength) assigned to a role, or None when it is unset/disabled or its file is gone from the library.
    Silent None is the fail-safe for cosmetic roles; a caller that must be loud (the explicit add-on) checks itself."""
    r = (_macros().get("roles") or {}).get(name)
    if not isinstance(r, dict) or not r.get("enabled"):
        return None
    fn = str(r.get("file") or "")
    f = _lora_path(fn)
    if f is None or not (fn in (_lora_lib() or {}) or f.is_file()):
        return None
    return (fn, max(0.0, min(2.0, float(r.get("strength", 1.0)))))


def _macro_loras(macro: dict) -> list[tuple[str, float]]:
    """A macro's LoRA stack as (file, strength): {role:...} refs resolved through _role_lora, {file,strength} refs
    taken directly; unresolved refs dropped, files deduped (first strength wins)."""
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in (macro.get("loras") or []):
        if not isinstance(item, dict):
            continue
        if item.get("role"):
            got = _role_lora(str(item["role"]))
            if got is None:
                continue
            fn, st = got
        elif item.get("file"):
            fn = str(item["file"])
            st = max(0.0, min(2.0, float(item.get("strength", 1.0))))
        else:
            continue
        if fn in seen:
            continue
        seen.add(fn)
        out.append((fn, st))
    return out


def _lora_is_explicit(fn: str, e: dict | None) -> bool:
    """Is this add-on 18+? The owner's own mark wins ("explicit", the per-LoRA toggle they flip to show/hide); so an
    add-on is auto-hidden by default when 18+ is off and can be flipped visible any time. Otherwise it's guessed from
    Civitai's NSFW flag ("nsfw", recorded when added or looked up since) or explicit words in its name/file/triggers."""
    e = e or {}
    if isinstance(e.get("explicit"), bool):
        return e["explicit"]
    if e.get("nsfw") is True:
        return True
    if e.get("source") == "civitai" and "nsfw" not in e:
        return True                                      # not looked up yet: counted as 18+ until it is (fail safe)
    words = " ".join([str(e.get("name") or ""), fn, *[str(t) for t in (e.get("triggers") or [])]])
    return _words_explicit(re.sub(r"[_\-.]+", " ", words))


def _lora_entries() -> list[dict]:
    """The library: one row per .safetensors file. A file with no record is listed with family 'unknown'."""
    lib = _lora_lib()
    out = []
    files = sorted(f.name for f in COMFY_LORAS.glob("*.safetensors")) if COMFY_LORAS.is_dir() else []
    for fn in files:
        e = lib.get(fn)
        if e:
            out.append({"file": fn, "name": e.get("name") or fn, "family": e.get("family") or "unknown",
                        "triggers": e.get("triggers") or [], "strength": e.get("strength", 1.0),
                        "always_on": bool(e.get("always_on")), "pair": e.get("pair"), "role": e.get("role"),
                        "source": e.get("source", ""), "size": e.get("size") or (COMFY_LORAS / fn).stat().st_size,
                        "explicit": _lora_is_explicit(fn, e)})
        else:
            out.append({"file": fn, "name": re.sub(r"\.safetensors$", "", fn), "family": "unknown", "triggers": [],
                        "strength": 1.0, "always_on": False, "pair": None, "role": None, "source": "",
                        "size": (COMFY_LORAS / fn).stat().st_size, "explicit": _lora_is_explicit(fn, None)})
    return out


_NSFW_LOOKUP_BUSY = threading.Lock()


def _lora_nsfw_backfill(limit: int = 40) -> None:
    """11-19: add-ons added from Civitai before the 18+ mark existed get Civitai's NSFW flag, looked up once each, in
    the background (off the phone's request). Never raises; one that can't be looked up is tried again next time."""
    if not _NSFW_LOOKUP_BUSY.acquire(blocking=False):
        return
    try:
        todo = [(fn, e) for fn, e in _lora_lib().items()
                if e.get("source") == "civitai" and "nsfw" not in e
                and re.match(r"civitai:\d*@\d+$", str(e.get("source_id") or ""))][:limit]
        found = {}
        for fn, e in todo:
            version = str(e["source_id"]).split("@", 1)[1]
            try:
                v = _api_json(f"{CIVITAI_API}/model-versions/{version}", timeout=15)
                found[fn] = bool((v.get("model") or {}).get("nsfw"))
            except Exception:                            # noqa: BLE001 - offline, removed from Civitai: try later
                continue
        if not found or not LORA_LOCK.acquire(timeout=30):
            return
        try:
            with LIB_LOCK:
                lib = _lora_lib_for_update()             # fresh read: the lookups above took a while
                for fn, flag in found.items():
                    if isinstance(lib.get(fn), dict):
                        lib[fn]["nsfw"] = flag
                _lora_lib_write(lib)
        finally:
            LORA_LOCK.release()
    except Exception as e:                               # noqa: BLE001
        log.warning("add-on NSFW lookup failed: %s", e.__class__.__name__)
    finally:
        _NSFW_LOOKUP_BUSY.release()


def _family_model_name(family: str) -> str:
    for r in CREATE_RECIPES.values():
        if r.get("lora_family") == family:
            return r["name"]
    return {"qwen-edit": "Qwen photo edits", "wan22": "Wan 2.2 video"}.get(family, family)


def _cap_pairs(picked: list, limit: int = 4) -> list:
    """Keep add-ons in order while they fit `limit`: a video pair (both files) weighs 2 and is never split, anything
    else weighs 1. One that does not fit is left out and the walk goes on. `picked` is [(file, strength, entry)]."""
    kept, total, done = [], 0, set()
    for item in picked:
        pid = item[2].get("pair")
        if pid:
            if pid in done:
                continue
            done.add(pid)
            group = [x for x in picked if x[2].get("pair") == pid]
            weight = 2
        else:
            group, weight = [item], 1
        if total + weight <= limit:
            kept.extend(group)
            total += weight
    return kept


def _job_loras(requested, family, extra_always_on: bool = True, allow_explicit: bool | None = None):
    """The LoRAs to use for a job of `family`, plus their trigger words. `requested` is the phone's list
    ([{name, strength}] or file names). Keeps installed LoRAs of the right family (plus 'unknown' ones the user
    chose), adds the family's always-on LoRAs, caps a job at 4 and strength at 0-2. Returns (loras, triggers):
    loras is [(file, strength)] for a picture/edit, or [(high, low, strength)] pairs for wan22 video."""
    if not family:
        return ([], [])
    lib = {e["file"]: e for e in _lora_entries()}
    want, strengths, trig_over = [], {}, {}
    for item in requested or []:
        if isinstance(item, dict):
            nm, st = item.get("name") or item.get("file"), item.get("strength")
            if nm in lib and isinstance(item.get("triggers"), list):   # the phone edited this add-on's words:
                trig_over[nm] = [str(t) for t in item["triggers"] if str(t).strip()]   # use exactly these (may be none)
        else:
            nm, st = item, None
        if nm in lib:
            want.append(nm)
            if st is not None:
                strengths[nm] = st
    picked, seen = [], set()

    # 11-19: no 18+ add-ons with 18+ off on the phone, nor ever on an everyday model (Qwen-Image 2.1, the plain
    # editor: a caller passes allow_explicit=False for those, or True for the explicit editor).
    if allow_explicit is None:
        allow_explicit = not (family == "qwen21" and "qwen21" not in _adult_models())
    safe = getattr(_TL, "safe", False) or not allow_explicit

    def add(fn):
        if fn in seen or fn not in lib:
            return
        seen.add(fn)
        e = lib[fn]
        if safe and e.get("explicit"):
            return
        s = max(0.0, min(2.0, float(strengths.get(fn, e.get("strength", 1.0)))))
        picked.append((fn, s, e))
    for fn in want:                                      # the user's chosen ones, of this family or unknown
        if lib[fn].get("family") in (family, "unknown"):
            add(fn)
            pid = lib[fn].get("pair")
            if pid:                                      # the phone names one file of a video pair: use both
                for other, e in lib.items():
                    if other != fn and e.get("pair") == pid:
                        if fn in strengths:
                            strengths.setdefault(other, strengths[fn])
                        add(other)
    if extra_always_on:
        for fn, e in lib.items():
            if e.get("always_on") and e.get("family") == family:
                add(fn)
    picked = _cap_pairs(picked, int(_tune("lora_cap", 4)))
    triggers: list[str] = []
    for fn, _, e in picked:
        for t in (trig_over[fn] if fn in trig_over else (e.get("triggers") or [])):
            if t and t not in triggers:
                triggers.append(t)
    if family == "wan22":
        pairs, bypair = [], {}
        for fn, s, e in picked:
            pid = e.get("pair")
            if not pid:
                pairs.append((fn, fn, s))                # a single-file Wan LoRA loads on both experts
                continue
            d = bypair.setdefault(pid, {"high": None, "low": None, "strength": s})
            role = e.get("role")
            if role == "high" or (role not in ("high", "low") and d["high"] is None):
                d["high"] = fn
            else:
                d["low"] = fn
            d["strength"] = s
        for d in bypair.values():
            pairs.append((d["high"], d["low"], d["strength"]))
        return pairs, triggers
    return [(fn, s) for fn, s, _ in picked], triggers


def _with_triggers(prompt: str, triggers: list) -> str:
    """Add each trigger word to the prompt unless it is already there (case-insensitive)."""
    p = (prompt or "").strip()
    low = p.lower()
    extra = [t for t in (triggers or []) if t and t.lower() not in low]
    if not extra:
        return p or prompt or ""
    return (p.rstrip(".") + ((". " if p else "") + ", ".join(extra))).strip()


def _header_is_qwen21(header: dict | None) -> bool:
    """True only when a parsed safetensors header positively says Qwen-Image 2.1."""
    if not isinstance(header, dict):
        return False
    meta = header.get("__metadata__", {})
    meta = meta if isinstance(meta, dict) else {}
    kj = " ".join(k for k in header if k != "__metadata__").lower()
    arch = str(meta.get("modelspec.architecture") or "").lower()
    base = str(meta.get("ss_base_model_version") or "").lower()
    return bool("qwen-image-2.1/lora" in kj or "qwen_image_21" in kj or "qwen-image-2.1" in arch
                or ("2.1" in base and "qwen" in base))


def _read_lora_header(path: Path) -> dict | None:
    try:
        with open(path, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if n <= 0 or n > SAFE_HEADER_MAX:
                return None
            h = json.loads(f.read(n).decode("utf-8"))
        return h if isinstance(h, dict) else None
    except Exception:                                    # noqa: BLE001
        return None


def _resort_qwen_loras() -> None:
    """Add-ons saved before the 2.1 check worked were all filed as qwen-edit. Look once at each one's file and move
    it to qwen21 on positive evidence only. Changes nothing but family and family_checked; never raises."""
    if not LORA_LOCK.acquire(blocking=False):
        return
    try:
        with LIB_LOCK:                                   # 31: read, change, write as one step
            lib = _lora_lib_for_update()
            changed = False
            for fn, e in lib.items():
                if not isinstance(e, dict) or e.get("family") != "qwen-edit" or "family_checked" in e:
                    continue
                path = COMFY_LORAS / fn
                if not path.is_file():
                    continue
                if _header_is_qwen21(_read_lora_header(path)):
                    e["family"] = "qwen21"
                e["family_checked"] = True
                changed = True
            if changed:
                _lora_lib_write(lib)
    except Exception:                                    # noqa: BLE001
        pass
    finally:
        LORA_LOCK.release()


def _resolve_family(label: str, header: dict | None) -> tuple[str | None, str | None]:
    """(family, warning) from a source label then the file header (metadata, then key names), in that order.
    (None, reason) when it doesn't fit any model here; (None, None) when it stays unknown and the card must ask."""
    meta = (header or {}).get("__metadata__", {}) if isinstance(header, dict) else {}
    keys = [k for k in (header or {}) if k != "__metadata__"]
    kj = " ".join(keys).lower()
    arch = str(meta.get("modelspec.architecture") or "").lower()
    base = str(meta.get("ss_base_model_version") or "").lower()
    qwen21_file = _header_is_qwen21(header)
    L = (label or "").strip().lower()
    if L:
        if L == "sdxl 1.0":
            return "sdxl", None
        if L in ("pony", "illustrious", "noobai"):
            return "sdxl", LORA_WARN_RELATED
        if L == "chroma":
            return "chroma", None
        if "wan" in L and "2.2" in L:
            if "5b" in L:
                return None, LORA_WAN_5B
            return "wan22", None
        if "qwen" in L:
            return ("qwen21", None) if ("2.1" in L or qwen21_file) else ("qwen-edit", None)
        return None, LORA_NO_FIT
    if "qwen-image-2.1/lora" in kj or "qwen_image_21" in kj:
        return "qwen21", None
    if any(k.startswith("diffusion_model.blocks.") and ".cross_attn" in k for k in keys):
        return "wan22", None
    if any(s in kj for s in ("lora_unet_input_blocks", "lora_te1_", "lora_te2_")):
        return "sdxl", None
    if "sdxl" in arch or "sdxl" in base or "xl-base" in base:
        return "sdxl", None
    if "chroma" in arch or "chroma" in base:
        return "chroma", None
    if "qwen" in arch or "qwen" in base:
        return ("qwen21" if ("2.1" in base or "2.1" in arch) else "qwen-edit"), None
    if "wan" in arch or "wan" in base:
        return "wan22", None
    return None, None


def _wan_role(name: str) -> str | None:
    n = (name or "").lower()
    if "high" in n or re.search(r"(?:^|[^a-z])(?:hn|h)(?:[^a-z]|$)", n):
        return "high"
    if "low" in n or re.search(r"(?:^|[^a-z])(?:ln|l)(?:[^a-z]|$)", n):
        return "low"
    return None


def _stream_lora(url: str, dest: Path, emit, cancel, max_bytes: int = LORA_MAX_BYTES):
    """Download a safetensors file to `dest`, parsing and validating its header as it arrives. Returns
    (header dict, sha256 hex, size). Raises UserFacing on anything that isn't a safetensors file within the limits."""
    import hashlib
    r = _open_checked(url)
    try:
        cl = r.headers.get("Content-Length")
        if cl and int(cl) > max_bytes:
            r.close()                                    # not yet inside the try/finally below
            raise UserFacing("That add-on is too large (over 2 GB).")
    except (TypeError, ValueError):
        pass
    h = hashlib.sha256()
    head = bytearray()
    header = None
    head_len = 8
    total = None
    try:
        total = int(r.headers.get("Content-Length") or 0) or None
    except (TypeError, ValueError):
        total = None
    got, last = 0, 0.0
    try:
        with open(dest, "wb") as f:
            while True:
                if cancel.is_set():
                    raise UserFacing("Stopped.")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > max_bytes:
                    raise UserFacing("That add-on is too large (over 2 GB).")
                h.update(chunk)
                f.write(chunk)
                if header is None:
                    head += chunk
                    if len(head) >= 8:
                        n = struct.unpack("<Q", bytes(head[:8]))[0]
                        if n <= 0 or n > SAFE_HEADER_MAX:
                            raise UserFacing("That file isn't a valid safetensors add-on.")
                        if len(head) >= 8 + n:
                            try:
                                header = json.loads(bytes(head[8:8 + n]).decode("utf-8"))
                            except Exception as e:       # noqa: BLE001
                                raise UserFacing("That file isn't a valid safetensors add-on.") from e
                            if not isinstance(header, dict):   # 'null' or a list: reject now, don't keep buffering
                                raise UserFacing("That file isn't a valid safetensors add-on.")
                            head_len = 8 + n
                now = time.monotonic()
                if total and now - last > 0.3:
                    last = now
                    emit({"t": "progress", "v": round(min(0.99, got / total), 3)})
    finally:
        r.close()
    if header is None:
        raise UserFacing("That file isn't a valid safetensors add-on.")
    if total and got != total:                           # the connection closed early: a cut-off file looks fine up front
        raise UserFacing("The download stopped before it finished. Please try again.")
    try:                                                 # the header says where the last tensor ends: the file must reach it
        end = max([int(v["data_offsets"][1]) for k, v in header.items()
                   if k != "__metadata__" and isinstance(v, dict) and v.get("data_offsets")] or [0])
    except (TypeError, ValueError, KeyError, IndexError):
        end = 0
    if got < head_len + end:
        raise UserFacing("The download stopped before it finished. Please try again.")
    return header, h.hexdigest(), got


def _parse_lora_link(link: str) -> dict:
    """A pasted link / urn / search-result id -> a source descriptor. Raises UserFacing on an unusable one."""
    import urllib.parse
    s = (link or "").strip()
    m = re.match(r"urn:air:[^:]*:[^:]*:civitai:(\d+)@(\d+)", s, re.I) or re.match(r"civitai:(\d+)@(\d+)$", s)
    if m:
        return {"kind": "civitai_version", "model": m.group(1), "version": m.group(2)}
    m = re.match(r"hf:(.+)$", s)
    if m:
        return {"kind": "hf_repo", "repo": m.group(1)}
    p = urllib.parse.urlsplit(s if "://" in s else "https://" + s)
    host, path = (p.hostname or "").lower(), p.path or ""
    qs = urllib.parse.parse_qs(p.query or "")
    if _is_civitai_host(host):
        m = re.search(r"/api/download/models/(\d+)", path) or re.search(r"/model-versions/(\d+)", path)
        if m:
            return {"kind": "civitai_version", "version": m.group(1)}
        m = re.search(r"/models/(\d+)", path)
        if m:
            return {"kind": "civitai_model", "model": m.group(1), "version": (qs.get("modelVersionId") or [None])[0]}
    if host in ("huggingface.co", "www.huggingface.co"):
        m = re.search(r"/([^/]+/[^/]+)/(?:blob|resolve)/[^/]+/(.+\.safetensors)$", path)
        if m:
            rev = re.search(r"/(?:blob|resolve)/([^/]+)/", path).group(1)   # the branch / tag / commit in the link
            return {"kind": "hf_file", "repo": m.group(1), "file": m.group(2), "rev": rev}
        m = re.match(r"/([^/]+/[^/]+)/?$", path)
        if m:
            return {"kind": "hf_repo", "repo": m.group(1)}
    if p.scheme in ("http", "https") and path.lower().endswith(".safetensors"):
        return {"kind": "direct", "url": "https://" + s.split("://", 1)[-1] if "://" in s else "https://" + s}
    if p.scheme in ("http", "https") and ("." in host):
        return {"kind": "page", "url": "https://" + s.split("://", 1)[-1] if "://" in s else "https://" + s}
    raise UserFacing("That doesn't look like a link to an add-on.")


def _civitai_file_rows(files: list) -> list[dict]:
    rows = []
    for f in files or []:
        if not str(f.get("name", "")).lower().endswith(".safetensors"):
            continue
        rows.append({"name": f.get("name"), "url": f.get("downloadUrl"),
                     "size": int((f.get("sizeKB") or 0) * 1024),
                     "sha256": str((f.get("hashes") or {}).get("SHA256") or "").lower(),
                     "pickle": f.get("pickleScanResult"), "virus": f.get("virusScanResult")})
    return rows


def _suggested_strength(text: str) -> float | None:
    """Best-effort guess at a LoRA's suggested weight, pulled from its Civitai description. Civitai has no
    structured field for this, so creators write it in free text -- this scans for it and is never
    authoritative. Returns a value in 0.1..2.0 rounded to the slider's 0.05 step, or None when nothing
    convincing is found. Looks for the <lora:name:0.8> prompt syntax first (the most reliable signal), then a
    number sitting next to the words "weight" or "strength"."""
    if not text:
        return None
    import html as _html
    raw = _html.unescape(str(text))                            # entities decoded; <lora:...> tags still intact
    t = re.sub(r"<[^>]+>", " ", raw)                           # tags stripped, for the keyword scans below

    def snap(x) -> float | None:
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return round(round(v / 0.05) * 0.05, 2) if 0.1 <= v <= 2.0 else None

    num = r"([012](?:\.\d+)?)"                                  # 0.x / 1.x / 2(.0); first digit capped so steps/CFG don't match
    kw = r"(?:recommended\s+|suggested\s+)?(?:lora\s+)?(?:weight|strength)"
    # 1) explicit prompt syntax, e.g. <lora:someStyle:0.8> -- scanned on raw text, before tags are stripped
    for m in re.finditer(rf"<lora:[^:>\s]+:\s*{num}\s*>", raw, re.I):
        v = snap(m.group(1))
        if v is not None:
            return v
    # 2) a range next to the keyword, e.g. "weight 0.6-0.8" or "0.6 to 0.8 strength" -> midpoint
    for pat in (rf"{kw}\D{{0,14}}{num}\s*(?:-|–|—|to)\s*{num}",
                rf"{num}\s*(?:-|–|—|to)\s*{num}\D{{0,14}}{kw}"):
        m = re.search(pat, t, re.I)
        if m:
            a, b = snap(m.group(1)), snap(m.group(2))
            if a is not None and b is not None:
                return round(round((a + b) / 2 / 0.05) * 0.05, 2)
    # 3) a single number next to the keyword, e.g. "weight: 0.8", "strength of 0.7", "0.8 weight"
    for pat in (rf"{kw}\D{{0,14}}{num}", rf"{num}\s*{kw}"):
        m = re.search(pat, t, re.I)
        if m:
            v = snap(m.group(1))
            if v is not None:
                return v
    return None


def _civitai_description(model_id: str, v: dict) -> str:
    """The free text where a suggested weight might live: the version notes, plus the model's own description
    (one extra read, guarded -- a failure here must never break an add)."""
    parts = [str(v.get("description") or "")]
    try:
        if model_id:
            parts.append(str(_api_json(f"{CIVITAI_API}/models/{model_id}").get("description") or ""))
    except Exception:
        pass
    return "\n".join(parts)


def _civitai_model(model_id: str) -> dict:
    """The model's own record (description, stats, creator, tags). Guarded — a failure must never break a lookup."""
    try:
        return _api_json(f"{CIVITAI_API}/models/{model_id}") if model_id else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _civitai_text(html: str, cap: int = 2000) -> str:
    """A Civitai description (HTML) as plain text: block tags -> newlines, <li> -> '- ', the rest stripped, blank
    runs collapsed, length capped. Untrusted creator text — the phone must show it as plain text, never as markup."""
    import html as _htmlmod
    s = re.sub(r"(?i)<br\s*/?>", "\n", html or "")
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = _htmlmod.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", s).strip()
    return (s[:cap].rstrip() + "…") if len(s) > cap else s


def _civitai_gallery(images: list, safe: bool, cap: int = 12) -> list:
    """Example images for the card: url, size, BlurHash placeholder and nsfw level. In safe mode only PG images
    (nsfwLevel <= 1) are kept, matching the existing thumbnail filter. Per-image generation meta is not in this
    payload (a separate withMeta call), so it is omitted here."""
    out = []
    for im in (images or []):
        if not isinstance(im, dict) or not im.get("url"):
            continue
        lvl = int(im.get("nsfwLevel") or 0)
        if safe and lvl > 1:
            continue
        out.append({"url": im["url"], "w": im.get("width"), "h": im.get("height"),
                    "nsfw_level": lvl, "hash": im.get("hash")})
        if len(out) >= cap:
            break
    return out


def _plan_from_descriptor(d: dict) -> dict:
    """A source descriptor -> a download plan {source, source_id, name, base, triggers, files:[...]} or a
    {choices:[...]} when the user must pick one of several files."""
    import urllib.parse
    k = d["kind"]
    if k in ("civitai_version", "civitai_model"):
        version = d.get("version")
        model = d.get("model")
        if k == "civitai_model" and not version:
            m = _api_json(f"{CIVITAI_API}/models/{model}")
            version = str(((m.get("modelVersions") or [{}])[0]).get("id") or "")
        v = _api_json(f"{CIVITAI_API}/model-versions/{version}")
        model = model or str(v.get("modelId") or "")
        rows = _civitai_file_rows(v.get("files"))
        if not rows:
            raise UserFacing("That add-on has no safetensors file.")
        name = (v.get("model") or {}).get("name") or v.get("name") or f"civitai-{version}"
        mj = _civitai_model(model)                       # 11-21: one guarded read, reused for description + card fields
        desc_raw = "\n".join([str(v.get("description") or ""), str(mj.get("description") or "")])
        st = mj.get("stats") or {}
        return {"source": "civitai", "source_id": f"civitai:{model}@{version}", "name": name,
                "nsfw": bool((v.get("model") or {}).get("nsfw")),      # 11-19: Civitai's own 18+ flag
                "base": v.get("baseModel", ""), "triggers": v.get("trainedWords") or [], "files": rows,
                "strength": _suggested_strength(desc_raw),
                "images": v.get("images") or [], "version": v.get("name") or "", "description": desc_raw,
                "url": f"https://civitai.com/models/{model}" if model else "",
                "rating": {"up": int(st.get("thumbsUpCount") or 0), "down": int(st.get("thumbsDownCount") or 0),
                           "downloads": int(st.get("downloadCount") or 0)},
                "creator": (mj.get("creator") or {}).get("username") or "",
                "tags": [str(t) for t in (mj.get("tags") or [])][:12]}
    if k in ("hf_repo", "hf_file"):
        repo = d["repo"]
        rev = urllib.parse.quote(urllib.parse.unquote(str(d.get("rev") or "main")), safe="")    # a link to a pinned version downloads that version
        if k == "hf_file":
            files = [d["file"]]
        else:
            info = _api_json(f"https://huggingface.co/api/models/{urllib.parse.quote(repo)}")
            files = [s["rfilename"] for s in (info.get("siblings") or [])
                     if str(s.get("rfilename", "")).lower().endswith(".safetensors")]
            if not files:
                raise UserFacing("That page has no safetensors add-on.")
            if len(files) > 1:
                return {"choices": files, "repo": repo, "source": "hf"}
        rows = [{"name": f.split("/")[-1],
                 "url": f"https://huggingface.co/{repo}/resolve/{rev}/" + urllib.parse.quote(f),
                 "size": None, "sha256": "", "pickle": None, "virus": None} for f in files]
        return {"source": "hf", "source_id": f"hf:{repo}", "name": repo.split("/")[-1], "base": "",
                "triggers": [], "files": rows}
    if k == "direct":
        url = d["url"]
        nm = urllib.parse.unquote(urllib.parse.urlsplit(url).path.split("/")[-1]) or "lora.safetensors"
        return {"source": "link", "source_id": url, "name": re.sub(r"\.safetensors$", "", nm, flags=re.I),
                "base": "", "triggers": [], "files": [{"name": nm, "url": url, "size": None, "sha256": "",
                                                       "pickle": None, "virus": None}]}
    if k == "page":
        html = _api_text(d["url"])
        links = list(dict.fromkeys(re.findall(r'https://[^\s"\'<>]+\.safetensors', html)))
        if not links:
            raise UserFacing("No add-on file was found on that page.")
        if len(links) > 1:
            return {"choices": [l.split("/")[-1] for l in links], "links": links, "source": "link"}
        return _plan_from_descriptor({"kind": "direct", "url": links[0]})
    raise UserFacing("That doesn't look like a link to an add-on.")


def job_lora_key(p: dict, emit, cancel):
    """Saves (or clears) the Civitai key. Never returns it."""
    key = str(p.get("key") or "").strip()
    if key:
        sec.write_text(CIVITAI_TOKEN, key)               # sealed at rest (DPAPI)
    else:
        CIVITAI_TOKEN.unlink(missing_ok=True)
    emit({"t": "info", "civitai_key": bool(key)})


def job_lora_list(p: dict, emit, cancel):
    _resort_qwen_loras()
    threading.Thread(target=_lora_nsfw_backfill, daemon=True, name="lora-nsfw").start()   # 11-19, once per add-on
    emit({"t": "info", "library": _lora_entries(), "civitai_key": bool(_civitai_token())})


def job_lora_set(p: dict, emit, cancel):
    """Change strength, always_on or family for one file (and, for a pair, both halves)."""
    fn = str(p.get("file") or "")
    f = _lora_path(fn)                                   # the phone's name: a bare add-on file name only
    if f is None:
        raise UserFacing("That add-on isn't on your computer.")
    with LIB_LOCK:                                       # 31: a fresh read, the change and the write as one step
        lib = _lora_lib_for_update()
        if fn not in lib and not f.is_file():
            raise UserFacing("That add-on isn't on your computer.")
        e = lib.get(fn) or {"name": re.sub(r"\.safetensors$", "", fn), "family": "unknown", "triggers": [],
                            "strength": 1.0, "always_on": False, "pair": None, "role": None, "source": "", "source_id": "",
                            "sha256": "", "size": f.stat().st_size}
        if "strength" in p:
            try:
                st = float(p["strength"]) if p["strength"] is not None else 1.0   # a strength of 0 is real (add-on off)
            except (TypeError, ValueError):
                st = 1.0
            e["strength"] = max(0.0, min(2.0, st))
        if "always_on" in p:
            e["always_on"] = bool(p.get("always_on"))
        if "family" in p and p.get("family") in LORA_FAMILIES:
            e["family"] = p["family"]
            e["family_checked"] = True                   # chosen by hand: the Qwen re-sort leaves it alone
        if isinstance(p.get("explicit"), bool):          # 11-19: the owner's own 18+ mark beats any guess
            e["explicit"] = p["explicit"]
        lib[fn] = e
        pair = e.get("pair")
        if pair:                                         # keep both halves of a Wan pair in step
            for k, v in lib.items():
                if v.get("pair") == pair and k != fn:
                    for field in ("strength", "always_on", "family", "explicit"):
                        if field in p:
                            v[field] = e[field]
        _lora_lib_write(lib)
    emit({"t": "info", "library": _lora_entries()})


def job_macros_get(p: dict, emit, cancel):
    """The role map + macros (read fresh), plus the LoRA library so the phone can offer choices."""
    emit({"t": "info", "macros": _macros(), "library": _lora_entries()})


def job_macros_set(p: dict, emit, cancel):
    """Replace the whole roles/macros document (the phone sends its full edited copy). Validates that every named
    file is on the computer and strengths are in range, and that macro ids are unique; writes atomically and echoes
    the stored copy back. Mirrors job_lora_set's shape."""
    roles_in, macros_in = p.get("roles"), p.get("macros")
    if not isinstance(roles_in, dict) or not isinstance(macros_in, list):
        raise UserFacing("That didn't look like a valid roles/macros document.")
    lib = _lora_lib() or {}

    def known(fn: str) -> bool:
        return _lora_path(fn) is not None and (fn in lib or _lora_path(fn).is_file())

    stored = {str(x.get("id") or "").strip(): x for x in (_macros().get("macros") or [])}   # the copy on disk now

    roles = {}
    for name, r in roles_in.items():
        if not isinstance(r, dict):
            continue
        fn = str(r.get("file") or "")
        if fn and not known(fn):
            fn = ""                                       # 11-21: a role pointing at a LoRA that isn't installed must
                                                          # NOT block saving macros (the built-in explicit role defaults
                                                          # to a file the owner may never have downloaded) -- unset it.
        roles[str(name)] = {"file": fn, "strength": max(0.0, min(2.0, float(r.get("strength", 1.0)))),
                            "enabled": bool(r.get("enabled")) and bool(fn)}
    macros, seen_ids = [], set()
    for m in macros_in:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid or mid in seen_ids:
            raise UserFacing("Each macro needs its own name.")
        seen_ids.add(mid)
        old = stored.get(mid)
        had = {str(x.get("file")) for x in ((old or {}).get("loras") or []) if isinstance(x, dict) and x.get("file")}
        loras = []
        for it in (m.get("loras") or []):
            if not isinstance(it, dict):
                continue
            if it.get("role"):
                loras.append({"role": str(it["role"])})
            elif it.get("file"):
                if not known(str(it["file"])) and str(it["file"]) not in had:
                    continue                              # 11-21: drop a NEWLY added add-on that isn't installed rather
                                                          # than refusing the whole save; one the macro already had stays
                                                          # (it may just be removed or not installed yet).
                loras.append({"file": str(it["file"]), "strength": max(0.0, min(2.0, float(it.get("strength", 1.0))))})
        macros.append({**(old or {}),                     # keep keys the phone does not edit (the Realism macro's tuning)
                       "id": mid, "name": str(m.get("name") or mid),
                       "prompt_add": str(m.get("prompt_add") or ""), "negative_add": str(m.get("negative_add") or ""),
                       "loras": loras, "settings": m.get("settings") if isinstance(m.get("settings"), dict) else {},
                       "model": (str(m["model"]) if m.get("model") else None), "explicit": bool(m.get("explicit")),
                       "pinned": bool(m.get("pinned"))})   # 11-22: one-tap favourite in Make
    _macros_write({"roles": roles, "macros": macros})
    emit({"t": "info", "macros": _macros(), "library": _lora_entries()})


def job_describe_photo(p: dict, emit, cancel):
    """"Match this photo": a photo in -> the prompt that would redraw it. The image is downscaled on the computer
    before it reaches the vision brain, to stay within its context window."""
    b64 = p.get("image")
    if not isinstance(b64, str) or not b64:
        raise UserFacing("No photo to match.")
    try:
        img = _image_in(b64)
    except Exception:                                    # noqa: BLE001
        raise UserFacing("That photo couldn't be read.")
    px = max(256, min(2048, int(_tune("describe_image_px", 1280))))
    img.thumbnail((px, px))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    emit({"t": "status", "v": "Looking at your photo…"})
    parsed, note = describe_photo(base64.b64encode(buf.getvalue()).decode(), p.get("safe") is True, cancel)
    if parsed is None:
        raise UserFacing(note or "The photo couldn't be described.")
    emit({"t": "info", "described": parsed})


def job_lora_remove(p: dict, emit, cancel):
    """Delete a file (or a whole pair) and its library entry."""
    fn = str(p.get("file") or "")
    if _lora_path(fn) is None:                           # the phone's name: a bare add-on file name only
        raise UserFacing("That add-on isn't on your computer.")
    with LIB_LOCK:                                       # an unreadable library refuses here, BEFORE any file is deleted.
        lib = _lora_lib_for_update()                     # No LORA_LOCK: removing while another add-on downloads is allowed;
                                                         # a row left for a file deleted under an add is never listed (_lora_entries)
    pair = (lib.get(fn) or {}).get("pair")           # only to find the pair; the entries are removed from a fresh read below
    targets = [k for k, v in lib.items() if v.get("pair") == pair] if pair else [fn]
    if fn not in targets:
        targets.append(fn)
    freed, stopped = 0, False
    for t in targets:                                # files first, outside the library lock (COMFY.stop can take a while)
        f = _lora_path(t)
        if f is None or not f.is_file():
            continue
        size = f.stat().st_size
        try:
            f.unlink()
        except PermissionError:
            if not stopped:                          # ComfyUI may hold the file open on Windows; release it
                COMFY.stop()
                stopped = True
            f.unlink(missing_ok=True)
        freed += size
    with LIB_LOCK:                                   # 31: drop the entries from a fresh read, so a concurrent edit stays
        lib = _lora_lib_for_update()
        for t in targets:
            lib.pop(t, None)
        _lora_lib_write(lib)
    COMFY.warmed.clear()
    emit({"t": "info", "removed": fn, "freed_gb": round(freed / 1024 ** 3, 2), "library": _lora_entries()})


def job_lora_search(p: dict, emit, cancel):
    """Browse Civitai or Hugging Face for LoRAs that fit `family`. Nothing is kept on disk."""
    import urllib.parse
    source = str(p.get("source") or "civitai").lower()
    family = str(p.get("family") or "")
    query = str(p.get("query") or "").strip()
    cursor = p.get("cursor")
    if source == "hf":
        base = {"sdxl": "stabilityai/stable-diffusion-xl-base-1.0"}.get(family)
        params = [("filter", "lora"), ("limit", "20"), ("sort", "downloads"), ("direction", "-1")]
        if base:
            params.append(("filter", f"base_model:adapter:{base}"))
        if query:
            params.append(("search", query))
        elif not base:
            params.append(("search", family))
        data = _api_json("https://huggingface.co/api/models?" + urllib.parse.urlencode(params))
        items = [{"source": "hf", "source_id": f"hf:{m.get('id')}", "name": m.get("id", ""),
                  "downloads": m.get("downloads"), "thumb": None, "triggers": []} for m in (data or [])
                 if not (p.get("safe") is True and _words_explicit(re.sub(r"[_\-./]+", " ", m.get("id") or "")))]
        emit({"t": "info", "loras": items, "cursor": None})
        return
    # Engine-scoped: browse AND typed search both stay inside the model family's Civitai base models, so a search on
    # Chroma returns Chroma add-ons, not Juggernaut. `families` (optional list from the phone) widens the scope to
    # several engines at once; it defaults to the one in use. The strict title `query` is kept broad by a tag fallback.
    fams = [f for f in (p.get("families") or [family]) if f in LORA_FAMILIES] or ([family] if family else [])
    bases, seen = [], set()
    for f in fams:
        for b in LORA_CIVITAI_BASE.get(f, []):
            if b not in seen:
                seen.add(b)
                bases.append(b)
    famset = set(fams)
    # Discovery facets (all optional, safe defaults): how to order, over what window, and a Civitai category tag.
    sort = {"popular": "Most Downloaded", "rated": "Highest Rated", "new": "Newest"}.get(
        str(p.get("sort") or "popular"), "Most Downloaded")
    period = {"day": "Day", "week": "Week", "month": "Month", "year": "Year",
              "all": "AllTime"}.get(str(p.get("period") or "").lower())
    category = str(p.get("category") or "").strip()        # a Civitai tag to browse (e.g. "character", "style")

    def _fits(base: str) -> bool:                          # which chosen engine a result belongs to (for a badge)
        f = _resolve_family(base or "", None)[0]
        if f in famset:
            return True
        return bool(f and f.startswith("qwen") and any(x.startswith("qwen") for x in famset))

    safe = p.get("safe") is True                         # 11-19: 18+ off on the phone -> no NSFW results or previews

    def _page(mode: str, term: str, cur) -> dict:
        params = [("types", "LORA"), ("types", "LoCon"), ("types", "DoRA"),   # 11-21: LoCon/DoRA were invisible before
                  ("nsfw", "false" if safe else "true"), ("limit", "20"), ("sort", sort)]
        if period:
            params.append(("period", period))              # a window turns a sort into "trending this week/month"
        if term:
            params.append((mode, term))                    # "query" matches the title, "tag" matches the concept
        if cur:
            params.append(("cursor", str(cur)))
        for b in bases:                                    # always scope to the chosen engine(s)
            params.append(("baseModels", b))
        return _api_json(f"{CIVITAI_API}/models?" + urllib.parse.urlencode(params))

    def _rows(data: dict) -> list:
        out = []
        for m in data.get("items", []):
            if safe and m.get("nsfw"):
                continue
            v = (m.get("modelVersions") or [{}])[0]
            rows = _civitai_file_rows(v.get("files"))
            if not rows:
                continue
            thumb = next((im.get("url") for im in (v.get("images") or [])
                          if im.get("url") and not (safe and (im.get("nsfwLevel") or 0) > 1)), None)
            base = v.get("baseModel", "")
            out.append({"source": "civitai", "source_id": f"civitai:{m.get('id')}@{v.get('id')}",
                        "name": m.get("name", ""), "base": base, "fits": _fits(base),
                        "size_mb": round(sum(r["size"] for r in rows) / 1024 ** 2, 1),
                        "downloads": (m.get("stats") or {}).get("downloadCount"),
                        "thumb": thumb, "triggers": v.get("trainedWords") or []})
        return out

    # The cursor carries the search mode across "load more", so a tag browse/fallback keeps paging as a tag search.
    mode, real_cursor = "query", cursor
    if isinstance(cursor, str) and cursor.startswith("tag::"):
        mode, real_cursor = "tag", cursor[5:]
    if mode == "tag":
        term = query or category                           # whichever drove this tag page (recomputed on load-more)
    elif query:
        term = query
    elif category:                                         # a category with no words is a tag browse
        mode, term = "tag", category
    else:
        term = ""                                          # nothing typed: just the engine's most-popular
    data = _page(mode, term, real_cursor)
    items = _rows(data)
    # A typed title search that finds nothing on the first page is retried as a concept tag (loose, like the website).
    if query and mode == "query" and not items and not real_cursor:
        mode, data = "tag", _page("tag", query, None)
        items = _rows(data)
    items.sort(key=lambda it: 0 if it.get("fits") else 1)  # family-appropriate first; the add step still checks fit
    next_cursor = (data.get("metadata") or {}).get("nextCursor")
    if next_cursor and mode == "tag":
        next_cursor = f"tag::{next_cursor}"
    emit({"t": "info", "loras": items, "cursor": next_cursor})


def job_lora_thumb(p: dict, emit, cancel):
    """Fetch a preview image through the PC and return a JPEG (kept in memory only). 11-21: `max` (128..1600) sets the
    longest side for a bigger preview or a gallery image; the default 256 keeps older phones' small thumbnails."""
    from PIL import Image
    url = str(p.get("url") or "")
    if not url:
        raise UserFacing("No preview to show.")
    try:
        mx = max(128, min(1600, int(p.get("max") or 256)))
    except (TypeError, ValueError):
        mx = 256
    r = _open_checked(url, timeout=20)
    try:
        raw = r.read((16 if mx >= 640 else 8) * 1024 * 1024)
    finally:
        r.close()
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((mx, mx), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88 if mx >= 640 else 85)
    emit({"t": "info", "thumb": base64.b64encode(buf.getvalue()).decode()})


def job_lora_inspect(p: dict, emit, cancel):
    """A link or a search result -> the confirmation card (name, size, which model it fits, triggers, warnings)."""
    d = _parse_lora_link(str(p.get("link") or p.get("source_id") or ""))
    plan = _plan_from_descriptor(d)
    if "choices" in plan:
        emit({"t": "info", "choices": plan["choices"], "repo": plan.get("repo"), "links": plan.get("links")})
        return
    fam, warn = _resolve_family(plan.get("base", ""), None)
    size = sum((f.get("size") or 0) for f in plan["files"])
    safe = p.get("safe") is True                         # 11-21: a safe-mode card hides R+ example images
    emit({"t": "info", "card": {
        "name": plan["name"], "source": plan["source"], "source_id": plan["source_id"],
        "size_mb": round(size / 1024 ** 2, 1) if size else None,
        "family": fam, "fits": _family_model_name(fam) if fam else None,
        "triggers": plan.get("triggers") or [], "files": [f["name"] for f in plan["files"]],
        "warning": warn, "needs_pick": fam is None and warn is None,
        # 11-21: richer discovery — old phone builds ignore these keys
        "description": _civitai_text(plan.get("description") or ""), "version": plan.get("version") or "",
        "gallery": _civitai_gallery(plan.get("images"), safe), "rating": plan.get("rating"),
        "creator": plan.get("creator"), "tags": plan.get("tags") or [], "url": plan.get("url"),
        "strength": plan.get("strength")}})


def job_lora_add(p: dict, emit, cancel):
    """Download and install a LoRA. One at a time (its own lock), off the picture queue, streaming progress."""
    with LORA_LOCK:
        d = _parse_lora_link(str(p.get("link") or p.get("source_id") or ""))
        plan = _plan_from_descriptor(d)
        if "choices" in plan:
            raise UserFacing("That page has several files — choose one first.")
        files = plan["files"]
        if _resolve_family(plan.get("base", ""), None)[1] == LORA_WAN_5B:
            raise UserFacing(LORA_WAN_5B)
        picked = str(p.get("family") or "")              # the model the user chose on the card, if any
        is_pair = picked == "wan22" and len(files) >= 2
        if not is_pair and len(files) > 1:
            files = files[:1]
        if is_pair and len(files) > 2:                   # a pair is exactly two files: the high-noise and low-noise half
            hi = next((x for x in files if _wan_role(x["name"]) == "high"), None)
            lo = next((x for x in files if _wan_role(x["name"]) == "low"), None)
            files = [hi, lo] if hi and lo else files[:2]
        pair_id = secrets.token_hex(4) if is_pair else None
        _ensure_lora_store()
        added = []                                       # 31: the library is read fresh AFTER the download, not held from here
        warn_out = None
        for f in files:                                  # check every file before downloading any
            if plan["source"] == "civitai" and (f.get("pickle") not in (None, "Success")
                                                or f.get("virus") not in (None, "Success")):
                raise UserFacing("That file hasn't passed Civitai's safety scan, so nothing was added.")
            if f.get("size") and f["size"] > LORA_MAX_BYTES:
                raise UserFacing("That add-on is too large (over 2 GB).")
            if not str(f["name"]).lower().endswith(".safetensors"):
                raise UserFacing("Only .safetensors add-ons are allowed.")
        staged = []                                      # (part file, final name, row, family, sha, size): nothing is
        for f in files:                                  # installed until every file of the add-on has arrived
            clean = _clean_lora_name(f["name"])
            part = COMFY_LORAS / (clean + ".part" if not staged else f"{clean}.{len(staged)}.part")
            try:
                emit({"t": "status", "v": "Downloading the add-on…"})
                header, sha, size = _stream_lora(f["url"], part, emit, cancel)
                if cancel.is_set():
                    raise UserFacing("Stopped.")
                if f.get("sha256") and f["sha256"] != sha:
                    raise UserFacing("The download didn't match its checksum, so nothing was added.")
                fam, fam_warn = _resolve_family(plan.get("base", ""), header)
                fam = "qwen21" if (fam == "qwen21" and picked == "qwen-edit") else (picked or fam)
                if fam is None:
                    part.unlink(missing_ok=True)
                    raise UserFacing(fam_warn or "Pick the model this add-on is for, then add it again.")
                warn_out = warn_out or fam_warn
                final = COMFY_LORAS / f"{clean}-{sha[:8]}.safetensors"
                staged.append((part, final, f, fam, sha, size))
            except BaseException:
                part.unlink(missing_ok=True)
                for sp in staged:                        # a later file failed: the earlier halves go too
                    sp[0].unlink(missing_ok=True)
                raise
        placed = []
        try:
            for part, final, f, fam, sha, size in staged:
                existed = final.exists()
                part.replace(final)
                if not existed:
                    placed.append(final)
        except BaseException:
            for sp in staged:
                sp[0].unlink(missing_ok=True)
            for fp in placed:
                fp.unlink(missing_ok=True)
            raise
        new_rows = {}
        for part, final, f, fam, sha, size in staged:
            role = _wan_role(f["name"]) if fam == "wan22" and is_pair else None
            seed = plan.get("strength")                  # a weight guessed from the Civitai description, if any
            new_rows[final.name] = {"name": plan["name"], "family": fam, "source": plan["source"],
                                    "source_id": plan.get("source_id", ""), "sha256": sha, "size": size,
                                    "triggers": plan.get("triggers") or [],
                                    "strength": float(seed) if seed else 1.0, "pair": pair_id,
                                    "role": role, "always_on": False}
            if plan["source"] == "civitai":
                new_rows[final.name]["nsfw"] = bool(plan.get("nsfw"))
            added.append({"file": final.name, "family": fam})
        try:
            with LIB_LOCK:                               # 31: merge into the current library, so an edit or a removal
                lib = _lora_lib_for_update()             # made while this downloaded is kept
                lib.update(new_rows)
                _lora_lib_write(lib)
        except BaseException:                            # the list can't be saved: don't leave files it doesn't know
            for fp in placed:
                fp.unlink(missing_ok=True)
            raise
        if not QUEUE.busy():                             # never under a picture or video that is being drawn
            COMFY.stop()                                 # so the next job's ComfyUI sees the new file
        emit({"t": "info", "added": added, "warning": warn_out, "library": _lora_entries()})


def _no_job(p: dict, emit, cancel):
    raise RuntimeError("answered on the event loop")


JOBS = {"status": job_status, "chat": job_chat, "image": job_image, "video": job_video, "search": job_search,
        "cutout": job_cutout, "upscale": job_upscale, "forget": job_forget,
        "remove_model": job_remove_model, "installed": job_installed, "warm": job_warm,
        "comfy_recipes": job_comfy_recipes, "comfy_run": job_comfy_run, "config": job_config,
        "make_recipes": job_make_recipes,
        "lora_search": job_lora_search, "lora_thumb": job_lora_thumb, "lora_inspect": job_lora_inspect,
        "lora_add": job_lora_add, "lora_list": job_lora_list, "lora_set": job_lora_set,
        "lora_remove": job_lora_remove, "lora_key": job_lora_key,
        "macros_get": job_macros_get, "macros_set": job_macros_set, "describe_photo": job_describe_photo,
        "analyze_macro": job_analyze_macro, "recommend_macros": job_recommend_macros, "song": job_song,
        "song_write": job_song_write, "album": job_album, "artwork": job_artwork,
        "stop_all": _no_job, "queue": _no_job, "free_memory": _no_job,   # these answer on the event loop
        "leave": _no_job, "uninstall": job_uninstall}
# 11-17: never wait for the line, stream as before. (comfy_recipes never needed the card: it waited behind videos and
# left Make on "Asking your computer..." for minutes.)
IMMEDIATE = {"status", "search", "forget", "config", "comfy_recipes", "make_recipes", "chat", "installed",
             "song_write", "uninstall", "macros_get",
             "lora_search", "lora_thumb", "lora_inspect",   # 11-21: read-only lookups, never queue behind a job
             "describe_photo", "analyze_macro", "recommend_macros"}   # brain prompt slot, like chat
ASYNC_OPS = {"queue", "stop_all", "free_memory", "leave"}


def _comfy_stop_now():
    """ComfyUI drops its queue and stops the picture or video it is drawing."""
    if "COMFY" not in globals() or not COMFY.running():
        return
    import urllib.request
    for path, body in (("/queue", b'{"clear": true}'), ("/interrupt", b"{}")):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{COMFY_PORT}{path}", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass


# ---------------------------------------------------------------- HTTP

GPU = asyncio.Lock()
KEYSTORE = Keys(KEYS)
REPLAY = ReplayGuard()
LAST_USE = time.monotonic()
LAST_CHAT = time.monotonic()          # the chat model holds the last conversation in memory until it stops
CHATS_ACTIVE = 0                      # chats answering right now (the idle stop never cuts one off)


# ---------------------------------------------------------------- the line (11-17)

LAST_SEEN = time.monotonic()          # 11-17: the last request of any kind from a phone (chat, job, warm-up, queue)
LEFT_AT: float | None = None          # 11-18: when a phone said "leave" (app went to the background); None = someone is here
HIDDEN_OPS = {"warm"}                 # make warm-ups: never listed, never noted, never wait
QUEUED_OPS = {"image", "video", "cutout", "upscale", "comfy_run", "remove_model", "warm", "song", "album", "artwork"}   # warm: for "make" only
RESULT_TYPES = {"image", "video", "mask", "audio", "cover", "album"}
STOP_TEXT = {"replaced": "Stopped: a newer request from the same place replaced it.", "cancelled": "Stopped.",
             "stop_all": "Stopped: everything on your computer was stopped.",
             "cleared": "Stopped: the work on your computer was cleared."}
_END = object()


def _ordinal(k: int) -> str:
    return "%d%s" % (k, "th" if 10 <= k % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(k % 10, "th"))


def _job_prompt(p: dict) -> str:
    v = p.get("values") or {}
    text = p.get("prompt") or v.get("change") or v.get("prompt") or ""
    return text if isinstance(text, str) else ""


def _job_kind(op: str, p: dict) -> str:
    if op in ("video", "cutout", "upscale", "warm", "song", "album", "artwork"):
        return op
    if op == "remove_model":
        return "remove"
    if op == "image":
        return "edit" if p.get("image") else "picture"
    v = p.get("values") or {}                            # comfy_run
    if v.get("image_mode") == "paint":
        return "change"
    if v.get("image") or v.get("images"):
        return "edit"
    try:
        return "pictures" if int(v.get("count") or 1) > 1 else "picture"
    except (TypeError, ValueError):
        return "picture"


def _job_label(op: str, p: dict) -> str:
    """A short plain label for the line; memory only. <= 60 chars + an ellipsis."""
    text = p.get("label") if isinstance(p.get("label"), str) else ""
    if not text and op == "remove_model":
        text = _brain_name(p.get("model") or "")
    text = " ".join((text or _job_prompt(p)).split())
    return text if len(text) <= 60 else text[:60] + "…"


class Job:
    """One picture/edit/video request. Lives in memory only, from submit until collected, stopped or 30 min after it
    finished (11-17: the phone may be suspended in the background when it ends)."""

    def __init__(self, op, payload, phone):
        self.id = secrets.token_hex(3)                       # 6 hex; re-rolled on a clash in submit
        self.op, self.payload, self.phone = op, payload, phone
        self.slot = payload.get("slot") or (None if op in ("warm", "remove_model") else f"legacy:{phone}")
        self.kind = _job_kind(op, payload)
        self.label = _job_label(op, payload)                 # memory only
        self.words = _job_prompt(payload)                    # full prompt; sent back only to the same phone
        self.hidden = op in HIDDEN_OPS
        self.enhance = _enhance_inputs(op, payload) is not None   # "Enhance my prompt" (11-17)
        self.enhance_done = threading.Event()
        if not self.enhance:
            self.enhance_done.set()
        self.enhanced: str | None = None                     # the brain's improved prompt (memory only)
        self.enhance_note: str | None = None                 # plain words when it could not
        self.enhance_s = 0.0
        self.enhance_state = "off"                           # off, on, failed
        self.acks = bool(payload.get("ack"))
        self.state = "waiting"                               # waiting, running, ready, failed, gone
        self.cancel = threading.Event()
        self.stop_reason: str | None = None
        self.created = time.monotonic()
        self.started = self.finished = None
        self.progress: float | None = None
        self.status = ""
        self.results: list[dict] = []                        # result frames (with "n"), dropped on ack/expiry
        self.error: str | None = None
        self.acked = False
        self.subs: set = set()
        self.loop = None                                     # set by submit
        self._pos: int | None = None                         # last position announced

    def emit(self, obj):                                     # from the job's thread
        self.loop.call_soon_threadsafe(self.publish, obj)

    def publish(self, obj):                                  # on the loop
        if self.stop_reason:                                 # stopped: nothing more goes out (or is kept)
            return
        t = obj.get("t")
        if t == "progress":
            self.progress = obj.get("v")
        elif t == "status":
            self.status = obj.get("v") or ""
        if t in RESULT_TYPES or (t == "info" and self.op == "remove_model"):
            obj = {**obj, "n": len(self.results) + 1}
            self.results.append(obj)
        for q in list(self.subs):
            q.put_nowait(obj)


class JobQueue:
    """The line of picture/edit/video jobs. Every change happens on the event loop thread."""

    def __init__(self):
        self.jobs: list[Job] = []            # in line order; held (ready/failed) ones are skipped by the views
        self.gone: dict[str, tuple[str, float]] = {}   # id -> (plain reason, when): ids only, no content, 2 h, <= 200
        self.wake: asyncio.Event | None = None

    # -- views
    def running(self) -> Job | None:
        return next((j for j in self.jobs if j.state == "running"), None)

    def waiting(self) -> list[Job]:
        return [j for j in self.jobs if j.state == "waiting"]

    def held(self) -> list[Job]:
        return [j for j in self.jobs if j.state in ("ready", "failed")]

    def busy(self) -> bool:
        return self.running() is not None

    def has_work(self) -> bool:
        return any(not j.hidden and j.state in ("waiting", "running", "ready", "failed") for j in self.jobs)

    def position(self, job: Job) -> int | None:
        if job.state == "running":
            return 0
        w = self.waiting()
        return w.index(job) + 1 if job in w else None

    def find(self, id_: str) -> Job | None:
        return next((j for j in self.jobs if j.id == id_), None)

    def next_kind(self) -> str | None:
        w = self.waiting()
        return w[0].kind if w else None

    def eta_running(self) -> int | None:
        j = self.running()
        if j is None or j.started is None or j.progress is None or j.progress < 0.05:
            return None
        return max(1, int((time.monotonic() - j.started) / j.progress * (1 - j.progress)))

    def snapshot(self, phone=None) -> list[dict]:
        now = time.monotonic()
        keep = _tune("keep_minutes", 30) * 60
        out = []
        ordered = [j for j in self.jobs if j.state == "running"] + self.waiting() + self.held()
        for j in ordered:
            if j.hidden:
                continue
            d = {"id": j.id, "kind": j.kind, "label": j.label, "state": j.state, "position": self.position(j),
                 "progress": j.progress if j.state == "running" else None, "status": j.status, "slot": j.slot,
                 "mine": phone is None or j.phone == phone, "age_s": int(now - j.created)}
            if j.state == "waiting":
                d["status"] = f"In line: {_ordinal(d['position'])}"
            if j.state == "running":
                eta = self.eta_running()
                if eta:
                    d["eta_s"] = eta
            if j.state in ("ready", "failed") and j.finished is not None:
                d["keep_s"] = max(0, int(j.finished + keep - now))
            if j.state == "failed":
                d["error"] = j.error
            out.append(d)
        return out

    # -- changes
    def _drop(self, job: Job, why: str | None):
        if job in self.jobs:
            self.jobs.remove(job)
        if why and not job.hidden:
            self.gone[job.id] = (why, time.monotonic())

    def _insert_waiting(self, job: Job, k: int | None):
        """Puts `job` at place k (0-based) among the waiting jobs; None = after the last."""
        w = self.waiting()
        if k is None or k >= len(w):
            self.jobs.append(job)
        else:
            self.jobs.insert(self.jobs.index(w[k]), job)

    def submit(self, job: Job, loop) -> list[Job]:
        """Returns the jobs it replaced. Raises UserFacing when the line is full."""
        job.loop = loop
        if not job.hidden and len([j for j in self.waiting() if not j.hidden]) >= int(_tune("queue_max", 30)):
            raise UserFacing("Your computer already has 30 things waiting. Stop some first.")
        while self.find(job.id) is not None or job.id in self.gone:
            job.id = secrets.token_hex(3)
        replaced: list[Job] = []
        place = None
        if not job.hidden:
            for j in list(self.jobs):                        # a warm-up never stands in anyone's way
                if j.hidden and j.state in ("waiting", "running"):
                    self.cancel(j, "replaced", _announce=False)
            if job.slot:
                w_before = self.waiting()
                replaced = [j for j in self.jobs if j.phone == job.phone and j.slot == job.slot and not j.hidden
                            and j.state in ("waiting", "running")]
                waiting_places = [w_before.index(j) for j in replaced if j.state == "waiting"]
                for j in replaced:
                    self.cancel(j, "replaced", _announce=False)
                if waiting_places:
                    place = min(waiting_places)              # it takes the replaced waiting job's place
                elif replaced:
                    place = 0                                # the running one went: the new one is first of the waiting
        if job.op == "remove_model":
            place = 0                                        # quick, and the owner is watching the model list
        self._insert_waiting(job, place)
        self._announce()
        if self.wake is not None:
            self.wake.set()
        return replaced

    def cancel(self, job: Job, reason: str = "cancelled", _announce: bool = True):
        if job.state == "gone" or job.stop_reason:
            return
        was = job.state
        job.stop_reason = reason
        job.results = []
        job.cancel.set()
        text = STOP_TEXT[reason] if reason in ("replaced", "stop_all", "cleared") else "It was stopped."
        if was == "running":
            # the runner removes it when its thread returns; ComfyUI runs one prompt at a time, so the interrupt
            # only hits this job
            if job.loop is not None:
                job.loop.run_in_executor(None, lambda: _comfy_stop_now())
            if not job.hidden:
                self.gone[job.id] = (text, time.monotonic())
        else:
            job.state = "gone"
            self._drop(job, text)
        for q in list(job.subs):
            q.put_nowait(_END)
        if _announce:
            self._announce()

    def cancel_slot(self, phone: str, slot: str) -> int:
        n = 0
        for j in list(self.jobs):
            if j.phone == phone and j.slot == slot and not j.hidden and j.state in ("waiting", "running"):
                self.cancel(j, "cancelled", _announce=False)
                n += 1
        self._announce()
        return n

    def move(self, job: Job, to: int) -> int:
        w = self.waiting()
        if job not in w:
            raise UserFacing("That job isn't waiting any more.")
        to = max(1, min(int(to), len(w)))
        self.jobs.remove(job)
        self._insert_waiting(job, to - 1)
        self._announce()
        return self.position(job)

    def stop_all(self, reason: str = "stop_all", drop_held: bool = True) -> int:
        n = 0
        for j in list(self.jobs):
            if j.state in ("waiting", "running"):
                self.cancel(j, reason, _announce=False)
                n += not j.hidden
            elif drop_held and j.state in ("ready", "failed"):
                self.cancel(j, reason, _announce=False)
        self._announce()
        return n

    def ack(self, job: Job):
        if job.state == "running":
            job.acked = True                                 # dropped the moment it ends
        elif job.state in ("ready", "failed"):
            self._drop(job, "Already collected.")

    def expire(self):
        now = time.monotonic()
        keep = _tune("keep_minutes", 30) * 60
        for j in list(self.held()):
            if j.finished is not None and j.finished + keep < now:
                self._drop(j, "It finished, but your computer only keeps finished work for "
                              f"{int(_tune('keep_minutes', 30))} minutes and it wasn't collected in time.")
        for k, (_, when) in list(self.gone.items()):
            if now - when > 7200:
                del self.gone[k]
        while len(self.gone) > 200:
            del self.gone[next(iter(self.gone))]

    def clear(self):
        """forget: everything, gone ids included."""
        self.stop_all("cleared", True)
        self.gone.clear()

    def _announce(self):
        for k, j in enumerate(self.waiting(), 1):
            if j._pos is None:
                j._pos = k                                   # first sight: the watcher's first frame says it
            elif j._pos != k:
                j._pos = k
                j.publish({"t": "queued", "id": j.id, "position": k})
                j.publish({"t": "status", "v": f"In line: {_ordinal(k)}"})

    def _finished(self, job: Job, err):
        """The job's thread has returned (on the loop). Decides: dropped now, or held for the phone to collect."""
        job.finished = time.monotonic()
        if job.stop_reason:                                  # stopped: cancel() already told everyone
            job.state = "gone"
            self._drop(job, None)
            job.results = []
            return
        watching = bool(job.subs)
        if err is not None:
            job.state, job.error = "failed", _plain(err)
        else:
            job.state = "ready"
        if job.hidden or (job.state == "ready" and not job.results):
            drop, why = True, None
        elif job.state == "failed":
            drop, why = watching, "It didn't work, and the reason was already shown."
        elif job.acks:
            drop, why = job.acked, "Already collected."
        else:
            drop, why = watching, "Already collected."
        if drop:
            self._drop(job, why)
        for q in list(job.subs):
            q.put_nowait(_END)
        self._announce()


QUEUE = JobQueue()


def _in_worker(op: str):
    """The diffusers helper route for `op` (the closure the old per-request code defined inline)."""
    def run(payload_, emit_, cancel_):
        if op == "video":
            VIDEO_BUSY.set()                           # a chat meanwhile answers from RAM, not the full card
            _room_for_video(lambda s: emit_({"t": "status", "v": s}))
        COMFY.free()                                   # ComfyUI's models leave the card first
        try:
            WORKER.run(op, payload_, emit_, cancel_)
        finally:
            if op == "video":
                VIDEO_BUSY.clear()
                if LLAMA.running() and not LLAMA.on_gpu:
                    LLAMA.stop()                       # next chat starts on the card again
    return run


def _target(op: str, payload: dict):
    """Which function runs this request (the routing that lived inside run_streaming)."""
    target = _in_worker(op) if op in WORKER_OPS else JOBS[op]
    safe = isinstance(payload, dict) and payload.get("safe") is True
    if (op == "image" and not payload.get("image") and COMFY.available()
            and (safe or _chat_recipe(payload.get("model")) or _picture_recipe(payload))):
        target = job_comfy_image                       # ComfyUI recipes for new pictures (11-19: always when 18+ off)
    if op == "video" and COMFY_VIDEO and COMFY.available() and _wan14_ready():
        target = job_comfy_video                       # Wan 2.2 A14B in ComfyUI (the 5B helper is the fallback)
    if op == "image" and payload.get("image") and _comfy_edits_ready():
        target = job_comfy_edit                        # every photo edit / remove: Qwen-Image-Edit / 2.1 in ComfyUI
    if op == "warm" and payload.get("for") != "make":
        target = job_brain_warm                        # app opened: the chat model first (old phones send no "for")
    elif op == "warm" and _chat_recipe(payload.get("model")) and COMFY.available():
        target = job_comfy_warm
    elif op == "warm" and _worker_alive() and getattr(WORKER, "family", None) == "video":
        target = job_warm_skip
    if safe and op == "image" and target not in (job_comfy_image, job_comfy_edit):
        target = _safe_no_fallback                     # 11-19: the old helper would run the saved (18+) model
    return target


def _safe_no_fallback(p: dict, emit, cancel):
    raise UserFacing("Your computer's picture tools aren't ready yet, and with 18+ off the older fallback can't be "
                     "used. Try again in a moment.")


def _safe_payload(op: str, p):
    """11-19: the phone's Explicit switch is off (it starts off every time the app opens): this job uses no explicit
    model, editor or add-on, whatever the chat model's call, Make's menus or the words say. The words are never
    changed. Old phones send no "safe" and keep the old behaviour."""
    if not isinstance(p, dict) or p.get("safe") is not True:
        return p
    p = dict(p)
    if op == "video":
        p["explicit"] = "off"
    elif op == "image":
        p["explicit"] = False
    vals = p.get("values")
    if isinstance(vals, dict):
        vals = dict(vals)
        vals["explicit"] = "Off"
        vals.pop("brain_explicit", None)
        if vals.get("editor") == "qwen-explicit":
            vals["editor"] = "qwen"
        p["values"] = vals
    return p


def _record_timing(job, rec: dict, err) -> None:
    """Keeps one finished job's numbers (memory only, no words). Warm-ups, removals and stopped jobs are left out."""
    if job.hidden or job.op == "remove_model" or job.stop_reason or job.started is None:
        return
    rec["total_s"] = round(time.monotonic() - job.started, 2)
    rec["ok"] = err is None
    rec["at"] = time.monotonic()
    rec["enhance"], rec["enhance_s"] = job.enhance_state, job.enhance_s
    TIMINGS.append(rec)


def _after_job(kind: str, next_kind: str | None) -> None:
    """11-17: after a video, give its ~38 GB back unless another video is next, then bring a paused brain back.
    11-19: and when nothing else is waiting, read the last picture model back in, in the background, so the next
    picture isn't a cold start (picture ~14 GB + brain ~20 GB fit the 64 GB the video just gave back)."""
    if kind == "video" and next_kind != "video":
        COMFY.free()                     # /free with free_memory also resets ComfyUI's node cache: the RAM goes
        rid = COMFY.last_picture
        if (next_kind is None and rid in CREATE_RECIPES and rid not in _adult_models() and _is_moe(LAST_BRAIN)
                and _tune("picture_ram_gb", 0) <= 0 and _recipe_ready(CREATE_RECIPES[rid])):
            threading.Thread(target=_rewarm_picture, args=(rid,), daemon=True, name="picture-rewarm").start()
    if LLAMA.paused_for and next_kind != "video":
        threading.Thread(target=LLAMA.resume, daemon=True, name="brain-resume").start()


def _rewarm_picture(rid: str) -> None:
    """11-19: after a video: a 1-step warm-up of the last picture recipe (never an 18+ one: _after_job checks). Gives
    way to anything that lined up meanwhile; a failure costs nothing (the next picture loads as before)."""
    try:
        if QUEUE.busy() or QUEUE.waiting() or LEFT_AT is not None:
            return                                     # a job is coming, or the owner has left: don't spend the load
        COMFY.ensure_current()
        cancel = threading.Event()
        asyncio.run(_comfy_run(_warmup_workflow(rid), lambda obj: None, cancel))
        COMFY.last_recipe = rid
    except Exception as e:                             # noqa: BLE001
        log.info("picture re-warm skipped: %s", e.__class__.__name__)


async def queue_runner(app):
    """11-17: one job on the card at a time, in line order, whether or not its phone is still connected."""
    QUEUE.wake = asyncio.Event()

    async def loop_():
        global LAST_USE
        loop = asyncio.get_running_loop()
        while True:
            if not QUEUE.waiting():
                QUEUE.wake.clear()
                await QUEUE.wake.wait()
                continue
            try:
                async with GPU:                              # the same lock idle unload / tray unload / free use
                    job = next(iter(QUEUE.waiting()), None)  # pick after the lock: the order may have changed
                    if job is None:
                        continue
                    job.state, job.started, job._pos = "running", time.monotonic(), 0
                    QUEUE._announce()
                    job.publish({"t": "status", "v": "Starting…"})
                    LAST_USE = time.monotonic()
                    payload = _safe_payload(job.op, job.payload)
                    err = None
                    rec = _new_timing_record(job.kind)
                    base = _target(job.op, payload)

                    def timed(payload_, emit_, cancel_, _base=base, _job=job, _rec=rec):
                        _TL.rec = _rec
                        _TL.safe = isinstance(payload_, dict) and payload_.get("safe") is True
                        try:
                            if _job.op == "cutout":
                                _timing(model=CUT_GENERAL)
                            elif _job.op == "upscale":
                                _timing(model=UPSCALER[0])
                            if (getattr(_job, "enhance", False) and not _job.enhance_done.is_set()
                                    and _base in (job_comfy_image, job_comfy_run, job_comfy_edit)):
                                _prepare_pictures(_base, payload_, emit_, cancel_)   # 11-19: alongside the words
                            payload_ = _apply_enhance(_job, payload_, emit_)
                            return _base(payload_, emit_, cancel_)
                        finally:
                            _TL.rec = None
                            _TL.safe = False
                    try:
                        await loop.run_in_executor(None, timed, payload, job.emit, job.cancel)
                    except Exception as e:                   # noqa: BLE001
                        log.warning("%s job failed: %s", job.op, e.__class__.__name__)   # never the content
                        err = e
                    finally:
                        job.payload = payload = None         # the photo and prompt go now
                        LAST_USE = time.monotonic()
                        _record_timing(job, rec, err)
                    await asyncio.sleep(0)                   # emits queued just before the end arrive first
                    QUEUE._finished(job, err)
                    try:
                        await loop.run_in_executor(None, _after_job, job.kind, QUEUE.next_kind())
                    except Exception:                        # noqa: BLE001
                        log.warning("after-job step failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:                           # noqa: BLE001 - the line must keep moving
                log.warning("the line's runner hit %s", e.__class__.__name__)
                await asyncio.sleep(1)

    task = asyncio.create_task(loop_())
    yield
    task.cancel()


async def _watch(job: Job, send, phone: str, reattach: bool = False):
    """Streams one job to one phone connection. The job never depends on this connection (11-17)."""
    q: asyncio.Queue = asyncio.Queue()
    job.subs.add(q)
    try:
        if not job.hidden:
            first = {"t": "queued", "id": job.id, "position": QUEUE.position(job), "kind": job.kind,
                     "slot": job.slot, "label": job.label}
            if reattach and job.phone == phone:
                first.update(words=job.words, state=job.state)
            await send(first)
        if job.enhanced:                           # a watcher that came after the improved prompt was announced
            await send({"t": "enhanced", "v": job.enhanced})
        for r in list(job.results):                # results published before this watcher (re-attach)
            await send(r)
        if job.state == "running":
            if job.status:
                await send({"t": "status", "v": job.status})
            if job.progress is not None:
                await send({"t": "progress", "v": job.progress})
        elif job.state == "waiting" and not job.hidden:
            await send({"t": "status", "v": f"In line: {_ordinal(QUEUE.position(job) or 1)}"})
        if job.state in ("waiting", "running") and not job.stop_reason:
            while True:
                try:
                    obj = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    await send({"t": "ping"})
                    continue
                if obj is _END:
                    break
                await send(obj)                    # live frames, results included (they carry "n")
        if job.stop_reason:
            await send({"t": "info", "stopped": job.stop_reason})
            await send({"t": "error", "v": STOP_TEXT.get(job.stop_reason, "Stopped.")})
        elif job.state == "failed":
            await send({"t": "error", "v": job.error})
        else:
            await send({"t": "done"})
    except asyncio.CancelledError:
        raise                                      # (finally still detaches)
    except (ConnectionResetError, asyncio.TimeoutError, OSError):
        pass                                       # phone gone or suspended: the job carries on / stays held
    finally:
        job.subs.discard(q)


def _brain_name(repo: str) -> str:
    """"llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-...-GGUF" -> "Qwen3.6-35B-A3B"."""
    name = (repo or "").split("/")[-1]
    name = re.sub(r"-GGUF$", "", name, flags=re.I)
    return re.sub(r"-uncensored.*$", "", name, flags=re.I)


async def _op_queue(payload: dict, send, phone: str) -> bool:
    """Answers one `queue` request. True = it already sent its own last frame (done or error)."""
    loop = asyncio.get_running_loop()
    action = payload.get("action")
    if action == "list":
        figs = await loop.run_in_executor(None, _figures)
        brain = {"state": LLAMA.state(), "why": LLAMA.paused_for, "model": _brain_name(LLAMA.repo or LAST_BRAIN),
                 "card_gb": round(LLAMA.card_gb, 1)}
        await send({"t": "info", "queue": {"jobs": QUEUE.snapshot(phone), **figs, "brain": brain,
                                           "keep_minutes": int(_tune("keep_minutes", 30)),
                                           "recent": _timing_recent(20), "averages": _timing_summary()}})
    elif action == "watch":
        wanted = str(payload.get("id") or "")
        job = QUEUE.find(wanted)
        if job is None:
            reason = QUEUE.gone.get(wanted, ("That job is no longer on your computer.",))[0]
            await send({"t": "info", "gone": reason})
            await send({"t": "error", "v": reason})
            return True
        await _watch(job, send, phone, reattach=True)
        return True                                # _watch ends with its own done/error
    elif action == "cancel":
        ids = payload.get("ids") or ([payload["id"]] if payload.get("id") else [])
        n = 0
        for i in ids:
            j = QUEUE.find(str(i))
            if j is not None:
                QUEUE.cancel(j)
                n += 1
        if payload.get("slot"):
            n += QUEUE.cancel_slot(phone, str(payload["slot"]))
        await send({"t": "info", "cancelled": n})
    elif action == "move":
        j = QUEUE.find(str(payload.get("id") or ""))
        if j is None or j.state != "waiting":
            await send({"t": "error", "v": "That job isn't waiting any more."})
            return True
        k = QUEUE.move(j, int(payload.get("to") or 1))
        await send({"t": "info", "moved": True, "position": k})
    elif action == "ack":
        j = QUEUE.find(str(payload.get("id") or ""))
        if j is not None:
            QUEUE.ack(j)
        await send({"t": "info", "acked": True})
    else:
        await send({"t": "error", "v": "Your computer doesn't know that request."})
        return True
    return False


async def _op_stop_all(payload: dict, send, phone: str) -> bool:
    n = QUEUE.stop_all("stop_all", drop_held=True)
    await asyncio.get_running_loop().run_in_executor(None, _comfy_stop_now)
    await send({"t": "info", "stopped": n})
    return False


def _unload_everything():
    LLAMA.resume_wanted = False
    LLAMA.paused_for = None
    LLAMA.stop()
    ENGINE.unload()                                    # also ends the helper process
    COMFY.free()                                       # ComfyUI keeps running, empty: no cold start next time


async def _op_free_memory(payload: dict, send, phone: str) -> bool:
    loop = asyncio.get_running_loop()
    n = QUEUE.stop_all("stop_all", True)
    await loop.run_in_executor(None, _comfy_stop_now)
    # 11-17: wait for the stopped job's thread to return, but keep the line to the phone alive: a thread inside a model
    # load cannot be interrupted and can take over 100 s, and the tunnel cuts a silent connection at 100 s.
    while True:
        try:
            await asyncio.wait_for(GPU.acquire(), 20)
            break
        except asyncio.TimeoutError:
            await send({"t": "ping"})
    try:
        await loop.run_in_executor(None, _unload_everything)
    finally:
        GPU.release()
    figs = await loop.run_in_executor(None, _figures)
    await send({"t": "info", "freed": True, "stopped": n, **figs})
    return False


async def _op_leave(payload: dict, send, phone: str) -> bool:
    """11-18: the phone app went to the background. Loads nothing and never waits for the line: it only starts the
    leave grace (see _leave_free_due). Any later request other than "leave" cancels it (handle)."""
    global LEFT_AT
    LEFT_AT = time.monotonic()
    await send({"t": "info", "leaving": True})
    return False


def _leave_free_due(now: float, left_at: float | None, grace: float, busy: bool, waiting: int, chats: int,
                    gpu_locked: bool) -> bool:
    """11-18: free the PC now? Only when a phone left, nobody came back since, the feature is on (grace > 0), the
    grace has passed, nothing runs or waits in the line, no chat is streaming and nothing holds the card. Held results
    do not count (they are bytes in RAM and stay held). A job running at leave time finishes first."""
    if left_at is None or grace <= 0:
        return False
    if busy or waiting or chats or gpu_locked:
        return False
    return now - left_at >= grace


def _leave_free():
    """The owner LEFT: Fixer has been off screen on his phone (backgrounded, switched away or locked) for his "free my
    computer after" time (default 5 min), nobody came back, and nothing runs or waits. 11-19: this now clears the
    graphics card and the chat model's RAM, but keeps the picture engine running with its models in RAM, so the next
    picture is seconds, not a cold engine start. ComfyUI itself stops (RAM returned) only after IDLE_UNLOAD (20 min)
    with no use at all (idle_unloader). The chat model reloads in seconds on return; the iPhone answers meanwhile.
    A paused chat model stays off."""
    ENGINE.unload()                                    # also ends the helper process
    LLAMA.stop_for_good()
    COMFY.unload_card()                                # off the card, kept in RAM; the engine keeps running


FREE_AFTER_MIN: float | None = None   # 11-18: the owner's "free my computer after" choice from the phone; None = unset
FREE_AFTER_SAVED: float | None = None  # 11-18: what free_after.json really holds (None = nothing saved / not read)


def _parse_free_after(v) -> float | None:
    """11-18: the phone's "free_after_min": a real number 0..1440, else None (ignored: bool, text, NaN, negative)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    if v != v or v < 0 or v > 1440:
        return None
    return v


def _free_after_file() -> Path:
    return ROOT / "free_after.json"


def _load_free_after() -> float | None:
    """11-18: the saved "free my computer after" choice (main() reads it at start, never the import, so the checks
    are not touched by a leftover file). Missing, unreadable or invalid = None (unset)."""
    try:
        data = json.loads(_free_after_file().read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return None
    return _parse_free_after(data.get("free_after_min")) if isinstance(data, dict) else None


def _save_free_after(choice: float) -> bool:
    """11-18: keeps the choice across restarts: only that number, written to a temp file then swapped in. On success
    FREE_AFTER_SAVED records what the file now holds; on failure it is left alone, so the next send tries again."""
    global FREE_AFTER_SAVED
    path = _free_after_file()
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"free_after_min": choice}), encoding="utf-8")
        os.replace(tmp, path)
        FREE_AFTER_SAVED = choice
        return True
    except Exception:                                  # noqa: BLE001
        log.warning("could not save the free-after choice")
        try:
            tmp.unlink()
        except Exception:                              # noqa: BLE001
            pass
        return False


def _leave_grace_s(choice: float | None, default_min: float) -> float:
    """11-18: seconds after a leave before the PC frees itself: his choice, else leave_free_minutes. 0 = never."""
    return (default_min if choice is None else choice) * 60


def _idle_limit_s(choice: float | None, idle_unload: float) -> float:
    """11-18: the idle rule's limit in seconds (also the backstop when no "leave" arrives). Unset: IDLE_UNLOAD.
    0 ("never"): never. Otherwise the longer of IDLE_UNLOAD and his choice, so "1 hour" really is an hour."""
    if choice is None:
        return idle_unload
    if choice <= 0:
        return float("inf")
    return max(idle_unload, choice * 60)


async def _leave_check(now: float | None = None) -> bool:
    """One pass of the leave rule (from the idle loop, every 30 s). True = it freed everything."""
    global LEFT_AT
    if LLAMA.starting:                                 # 11-18: a chat model start holds its lock for minutes; next pass
        return False
    grace = _leave_grace_s(FREE_AFTER_MIN, _tune("leave_free_minutes", 5))   # read live; 0 turns it off
    now = time.monotonic() if now is None else now
    if not _leave_free_due(now, LEFT_AT, grace, QUEUE.busy(), len(QUEUE.waiting()), CHATS_ACTIVE, GPU.locked()):
        return False
    async with GPU:
        if LEFT_AT is None or QUEUE.busy() or QUEUE.waiting():     # someone came back while we waited for the lock
            return False
        LEFT_AT = None                                 # once per leave
        await asyncio.get_running_loop().run_in_executor(None, _leave_free)
    return True


ASYNC_HANDLERS = {"queue": _op_queue, "stop_all": _op_stop_all, "free_memory": _op_free_memory, "leave": _op_leave}


def _drop_expired_pairings():
    """A temporary pairing past its expiry is deleted from keys.json, not just refused."""
    dropped = []

    def drop(data: dict):
        now = time.time()
        phones = data.get("phones", {})
        gone = [k for k, v in phones.items() if (v.get("expires") and float(v["expires"]) < now)
                or (v.get("pair_by") and float(v["pair_by"]) < now)]      # 11-19: a code nobody used in time
        if not gone:
            return False                               # nothing expired on the fresh copy: no write
        for k in gone:
            phones.pop(k)
        dropped.extend(gone)

    try:
        # 8: under the keys.json lock, from a fresh read (a confirmation that just landed is not undone); a file that
        # can't be read is left alone (and Keys.get has already logged it). Sealed at rest: never a plaintext copy.
        if sec.update_json(KEYS, drop, indent=1):
            log.warning("removed %d expired pairing(s)", len(dropped))
    except Exception:                                  # noqa: BLE001  (busy / unreadable: try again in 30 s)
        return


async def idle_unloader(app):
    """Unloads the image and video models (and any transformers fallback chat model) once nothing has used
    them for IDLE_UNLOAD seconds, and stops the llama.cpp chat model after the same idle time (it holds the last
    conversation in memory; restarting it costs a few seconds on the next chat).
    In use = any request in the last IDLE_UNLOAD (chat, job, the app-open warm-up, the Queue screen), or anything
    in the line or held for collection. The PC can't see the phone: a backgrounded phone is idle after 20 min."""
    async def loop():
        global LAST_USE
        while True:
            await asyncio.sleep(30)
            QUEUE.expire()
            # 11-19: was never called; unused 60 s pairing codes go too. In the executor: it can wait on keys.json.lock
            await asyncio.get_running_loop().run_in_executor(None, _drop_expired_pairings)
            if QUEUE.has_work():                   # queued/running jobs and held results count as use: never unload under them
                LAST_USE = time.monotonic()
            # Idle means no pictures AND no chat: a long chat between pictures no longer unloads the picture model
            # (the next picture then waited 25-110 s to load it again).
            idle = time.monotonic() - max(LAST_USE, LAST_CHAT, LAST_SEEN)
            limit = _idle_limit_s(FREE_AFTER_MIN, IDLE_UNLOAD)       # 11-18: the owner's choice stretches it or ends it
            if ENGINE.kind and not GPU.locked() and idle > limit:
                async with GPU:
                    await asyncio.get_running_loop().run_in_executor(None, ENGINE.unload)
            # The chat model too: its prompt cache holds the last conversation, so it never outlives the idle time.
            if LLAMA.running() and not GPU.locked() and CHATS_ACTIVE == 0 and idle > limit:
                await asyncio.get_running_loop().run_in_executor(None, LLAMA.stop)
            # ComfyUI holds the last photos, prompts and results in memory and its private folder: same idle rule.
            if COMFY.running() and not GPU.locked() and idle > limit:
                async with GPU:
                    await asyncio.get_running_loop().run_in_executor(None, COMFY.stop)
            # A pause can never outlive its video, even if the runner's after-job step was skipped by an error.
            try:
                await _leave_check()                   # 11-18: the phone left: free the PC after a short grace
            except Exception:                          # noqa: BLE001
                log.warning("leave free failed")
            if LLAMA.paused_for and not QUEUE.busy() and not QUEUE.waiting():   # (a leave-free above cleared it)
                threading.Thread(target=LLAMA.resume, daemon=True, name="brain-resume").start()
            await asyncio.get_running_loop().run_in_executor(None, _drop_expired_pairings)
    task = asyncio.create_task(loop())
    yield
    task.cancel()


def lower_priority():
    """Below-normal priority: games, Plex and everything else on the PC win."""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
        else:
            os.nice(10)
    except Exception:
        pass


def _deny() -> web.Response:
    return web.Response(status=401, text="no")


async def handle(request: web.Request) -> web.StreamResponse:
    global LAST_CHAT, CHATS_ACTIVE, LAST_SEEN, LEFT_AT, FREE_AFTER_MIN, FREE_AFTER_SAVED
    op = request.match_info["op"]
    if op not in JOBS:
        return web.Response(status=404)
    phone = request.headers.get("X-Fixer-Phone", "")
    stamp = request.headers.get("X-Fixer-Time", "")
    key = KEYSTORE.get(phone)
    if not key or not stamp.isdigit() or abs(time.time() - int(stamp)) > CLOCK_SKEW:
        return _deny()
    body = await request.read()
    aad = f"{PROTOCOL}|{op}|{phone}|{stamp}"
    try:
        payload = json.loads(unseal(key, body, aad.encode()))
    except Exception:
        return _deny()
    if not REPLAY.fresh(body[:12]):
        return _deny()
    del body
    if KEYSTORE.pending(phone):
        # 11-19: first use of a fresh code: it is now a real pairing. In the executor: it can wait on keys.json.lock
        await asyncio.get_running_loop().run_in_executor(None, confirm_pairing, phone)
    if op in ("leave", "warm") and isinstance(payload, dict):    # 11-18: the owner's "free my computer after" choice
        choice = _parse_free_after(payload.get("free_after_min"))
        if choice is not None:
            FREE_AFTER_MIN = choice
            # 11-18: survives a restart. Compared with what is really in the file, not with memory, so a save that
            # failed is tried again the next time the phone sends the value.
            if choice != FREE_AFTER_SAVED:
                _save_free_after(choice)

    resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream", "Cache-Control": "no-store"})
    await resp.prepare(request)
    if op != "leave":
        LAST_SEEN = time.monotonic()                   # every authenticated request is "in use" (idle stop)
        LEFT_AT = None                                 # 11-18: someone is back: no leave-free
    seq = 0

    async def send(obj: dict):
        nonlocal seq
        frame = seal(key, json.dumps(obj).encode(), f"{aad}|{seq}".encode())
        # 11-17: a phone suspended in the background stops reading; an unbounded write would hang on a full buffer
        # forever. At least ~200 KB/s, so a 10 MB video over a slow away connection isn't cut off.
        await asyncio.wait_for(resp.write(struct.pack(">I", len(frame)) + frame), timeout=30 + len(frame) / 200_000)
        seq += 1

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel = threading.Event()

    def emit(obj: dict):
        loop.call_soon_threadsafe(queue.put_nowait, obj)

    async def run_streaming():
        """Chat, status, search, forget, config: never wait for the line; stream the job's frames as they come."""
        target = job_brain_warm if op == "warm" else JOBS[op]       # app-open warm-up: the chat model
        fut = loop.run_in_executor(None, target, payload, emit, cancel)
        getter = asyncio.ensure_future(queue.get())
        while True:
            # Every 20 s of silence a "ping" frame (the phone ignores it): Cloudflare cuts a connection that says
            # nothing for 100 s, and a video's first model load can take that long.
            done, _ = await asyncio.wait({getter, fut}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                await send({"t": "ping"})
                continue
            if getter in done:
                await send(getter.result())
                getter = asyncio.ensure_future(queue.get())
                continue
            getter.cancel()
            await asyncio.sleep(0)                 # let emits queued just before the end arrive
            while not queue.empty():
                await send(queue.get_nowait())
            fut.result()                           # raises the job's error, if any
            return

    try:
        if op == "warm" and isinstance(payload, dict) and payload.get("for") == "settings":
            await send({"t": "info", "warm": False})     # 11-18: only records free_after_min (above); loads nothing
            await send({"t": "done"})
        elif op in ASYNC_OPS:
            if not await ASYNC_HANDLERS[op](payload, send, phone):
                await send({"t": "done"})
        elif op in QUEUED_OPS and not (op == "warm" and payload.get("for") != "make"):
            # 11-17: pictures, edits and videos line up (see JobQueue) and run whether or not this phone stays
            # connected: a backgrounded phone is suspended by iOS ~30 s later, and the job must not die with it.
            # A new request from the same place replaces that place's unfinished one (the old newest-wins).
            if op == "warm" and (QUEUE.busy() or QUEUE.waiting()):
                await send({"t": "info", "warm": False})
                await send({"t": "done"})
                return resp
            job = Job(op, payload, phone)
            payload = None                             # the Job owns it until it runs
            try:
                replaced = QUEUE.submit(job, loop)
            except UserFacing as e:
                await send({"t": "error", "v": _plain(e)})
                return resp
            if any(j.state == "running" for j in replaced):
                await send({"t": "status", "v": "Stopping the previous request…"})
            if job.enhance:                            # the brain improves the words while the job waits in line
                loop.run_in_executor(None, _enhance_job, job)
            await _watch(job, send, phone)
        else:
            if op == "forget":
                QUEUE.clear()                          # the line, held results and gone ids, before the rest
            if op == "chat":
                payload.pop("_queue", None)            # private keys: never taken from the phone
                payload.pop("_slot", None)
            if op == "warm" and isinstance(payload, dict):
                payload["_phone"] = phone              # 11-19: whose held results to count (never from the phone)
                payload.pop("_eta_s", None)
                payload["_queue"] = QUEUE.snapshot()
                payload["_eta_s"] = QUEUE.eta_running()
            # Chat runs in llama-server (its own process, already resident), so it never waits behind an image
            # job. The rare transformers fallback takes its own lock inside job_chat.
            LAST_CHAT = time.monotonic()
            CHATS_ACTIVE += 1
            try:
                await run_streaming()
            finally:
                CHATS_ACTIVE -= 1
                LAST_CHAT = time.monotonic()
            await send({"t": "done"})
    except (ConnectionResetError, asyncio.CancelledError):
        cancel.set()                               # phone went away: stop what is generating for it (not a queued job)
        raise
    except Exception as e:
        cancel.set()
        log.warning("%s job failed: %s", op, e.__class__.__name__)
        try:
            await send({"t": "error", "v": _plain(e)})
        except Exception:
            pass
    finally:
        payload = None
    return resp


def _plain(e: Exception) -> str:
    """One plain sentence for the phone."""
    name = e.__class__.__name__
    text = str(e).strip()
    if isinstance(e, UserFacing):
        return text
    if name == "OutOfMemoryError" or "out of memory" in text.lower():
        return "Not enough graphics memory. Try a smaller size or another model."
    if name == "GatedRepoError":
        return "This model needs a Hugging Face sign-in on your computer."
    if name in ("RepositoryNotFoundError", "RevisionNotFoundError", "EntryNotFoundError"):
        return "That model wasn't found on Hugging Face."
    if name in ("LocalEntryNotFoundError", "OfflineModeIsEnabled", "HfHubHTTPError", "ConnectionError",
                "ConnectTimeout", "ReadTimeout", "URLError", "ChunkedEncodingError"):
        return "Couldn't download the model. Check your computer's internet connection."
    return "Something went wrong on your computer. Try again."


LOCAL_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")


def _local_host_ok(host: str | None, port: int) -> bool:
    """11-18: True only for a Host header naming this PC by loopback: 127.0.0.1, localhost or [::1], with no port or
    exactly `port` (case-insensitive, one trailing dot allowed on the name). This is what defeats DNS rebinding: a
    hostile page that re-points its own name at 127.0.0.1 still sends that name here. Missing/empty/odd = False."""
    if not host or not isinstance(host, str) or host != host.strip() or any(c.isspace() for c in host):
        return False
    host = host.lower()
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return False
        name, rest = host[:end + 1], host[end + 1:]
    elif host.count(":") <= 1:
        name = host.partition(":")[0]
        rest = host[len(name):]
    else:
        return False
    if rest and rest != f":{int(port)}":
        return False
    if name.endswith("."):
        name = name[:-1]
    return name in LOCAL_HOSTNAMES


def _from_this_pc(request: web.Request) -> bool:
    # A tunnel (cloudflared) on this PC also connects from 127.0.0.1, but adds forwarding headers.
    # 11-18: a web page open in a browser on this PC can also reach 127.0.0.1; browsers add Origin to those requests.
    # 11-18: and a page using DNS rebinding sends no Origin on a GET but its own hostname as Host: Host must be loopback.
    forwarded = any(h in request.headers for h in ("Cf-Connecting-Ip", "X-Forwarded-For", "Forwarded", "Cf-Ray",
                                                   "Origin"))
    hosts = request.headers.getall("Host", [])
    if len(hosts) != 1 or not _local_host_ok(hosts[0], _own_port(request)):
        return False
    if not _url_host_local(request):                   # 11-18: an absolute request line naming another host
        return False
    return request.remote in ("127.0.0.1", "::1") and not forwarded


def _url_host_local(request: web.Request) -> bool:
    """11-18: request.url's host (taken from an absolute-form request line when there is one, else from Host) must be
    a loopback name too: 127.0.0.1, localhost or ::1 (yarl drops the brackets), one trailing dot allowed."""
    try:
        h = (request.url.host or "").lower()
    except Exception:                                  # noqa: BLE001  (an unparseable target is refused)
        return False
    if h.endswith("."):
        h = h[:-1]
    return h in ("127.0.0.1", "localhost", "::1")


def _own_port(request: web.Request) -> int:
    """The port this connection came in on (PORT for the real server; the checks' test server uses a spare one)."""
    try:
        return int(request.transport.get_extra_info("sockname")[1])
    except Exception:                                  # noqa: BLE001
        return PORT


async def local_state(request: web.Request) -> web.Response:
    """For the tray icon on this PC only: what's loaded and whether a job is running. No request content."""
    if not _from_this_pc(request):
        return web.Response(status=404)
    return web.json_response({"loaded": ENGINE.repo, "busy": QUEUE.busy(), "waiting": len(QUEUE.waiting()),
                              "idle_s": int(time.monotonic() - LAST_USE),
                              "pid": os.getpid(), "comfy": COMFY.running(), "llama": LLAMA.running(),   # 11-18
                              "free_after_min": FREE_AFTER_MIN,
                              # 11-18: true = proven at start, false = started without (knob 0), null = not running
                              "comfy_guarded": COMFY.guard_state(), "llama_keyed": LLAMA.key_state()})


# 11-18: the only fields /local/timings hands out. Names of models and recipes and numbers; never the prompt or photo.
TIMING_FIELDS = ("kind", "model", "recipe", "editor", "explicit", "mp", "steps", "count", "load_s", "draw_s",
                 "engine_start_s", "loaded", "runs", "enhance", "enhance_s", "total_s", "ok")


def _timings_report(now: float | None = None) -> dict:
    """11-18: the recent finished jobs (oldest first, newest last), each with its age in seconds instead of its
    monotonic stamp, plus when a phone left, the owner's free-after choice and the idle time. Only TIMING_FIELDS."""
    now = time.monotonic() if now is None else now
    jobs = []
    for r in list(TIMINGS):
        d = {k: r[k] for k in TIMING_FIELDS if k in r}
        for k in ("load_s", "draw_s", "total_s", "engine_start_s", "enhance_s", "mp"):
            if isinstance(d.get(k), float):
                d[k] = round(d[k], 2)
        d["age_s"] = int(now - r.get("at", now))
        jobs.append(d)
    return {"jobs": jobs, "left_s": None if LEFT_AT is None else int(now - LEFT_AT), "free_after_min": FREE_AFTER_MIN,
            "idle_s": int(now - max(LAST_USE, LAST_CHAT, LAST_SEEN))}


async def local_timings(request: web.Request) -> web.Response:
    """11-18: "why was that picture slow", from this PC only: GET /local/timings (see _timings_report)."""
    if not _from_this_pc(request):
        return web.Response(status=404)
    return web.json_response(_timings_report())


_EXIT = os._exit                                       # 11-18: the checks swap this out
QUIT_GRACE = 20.0                                      # seconds: the process ends by then, cleaned up or not
_QUITTING = threading.Event()


def _shutdown_cleanup():
    """What main()'s finally and /local/quit both run: the chat model, the helper and ComfyUI (which also empties its
    private folder). 11-18: their processes are killed first without waiting for their locks (a start in progress
    holds one for minutes), then each normal stop runs; one failing never skips the next."""
    for proc in (getattr(LLAMA, "proc", None), getattr(COMFY, "proc", None)):
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
        except Exception:                              # noqa: BLE001
            pass
    for step in (LLAMA.stop_for_good, WORKER.stop, COMFY.stop):
        try:
            step()
        except Exception:                              # noqa: BLE001
            log.warning("shutdown step failed")


async def _quit_now():
    """11-18: stops every job, then cleans up, then ends the process. A watchdog ends it after QUIT_GRACE seconds
    whatever happens (a thread stuck in a model load can neither be interrupted nor joined: a plain exit would wait for
    it forever). Children not stopped by then die with the server's job object (_JOB.own)."""
    loop = asyncio.get_running_loop()
    watchdog = threading.Timer(QUIT_GRACE, lambda: _EXIT(1))
    watchdog.daemon = True
    watchdog.start()
    await asyncio.sleep(0.2)                           # let the reply go out first
    QUEUE.stop_all("stop_all", drop_held=True)
    done = threading.Event()

    def work():
        try:
            _comfy_stop_now()
            _shutdown_cleanup()
        finally:
            done.set()
    threading.Thread(target=work, daemon=True, name="fixer-quit").start()   # not the executor: it may be full
    while not done.is_set() and watchdog.is_alive():
        await asyncio.sleep(0.1)
    watchdog.cancel()
    try:
        sys.stderr.flush()
        sys.stdout.flush()
    except Exception:                                  # noqa: BLE001
        pass
    _EXIT(0)


async def local_quit(request: web.Request) -> web.Response:
    """11-18: the tray's Turn off / Quit (this PC only, with the header X-Fixer-Local: 1, which a web page cannot send
    without the browser asking first): {"quitting": true}, then the server ends itself (_quit_now)."""
    if not _from_this_pc(request) or request.headers.get("X-Fixer-Local") != "1":
        return web.Response(status=404)
    if not _QUITTING.is_set():
        _QUITTING.set()
        _QUIT_TASK.append(asyncio.get_running_loop().create_task(_quit_now()))   # held: a bare task can be collected
    return web.json_response({"quitting": True})


_QUIT_TASK: list = []


async def local_unload(request: web.Request) -> web.Response:
    """Tray "Free GPU memory now" (this PC only)."""
    if not _from_this_pc(request):
        return web.Response(status=404)
    if not GPU.locked():
        async with GPU:
            await asyncio.get_running_loop().run_in_executor(None, ENGINE.unload)
            await asyncio.get_running_loop().run_in_executor(None, LLAMA.stop)   # restarts in seconds on the next chat
            # 11-19: the picture models too (it used to leave ComfyUI's ~15 GB in place), the same as the app's Free;
            # ComfyUI itself keeps running, empty, so the next picture has no cold engine start.
            await asyncio.get_running_loop().run_in_executor(None, COMFY.free)
    return web.json_response({"loaded": ENGINE.repo})


def app() -> web.Application:
    a = web.Application(client_max_size=MAX_BODY)
    a.cleanup_ctx.append(idle_unloader)
    a.cleanup_ctx.append(queue_runner)
    if COMFY.available():
        threading.Thread(target=lambda: _quietly(COMFY.ensure), daemon=True, name="comfy-start").start()
    a.router.add_post("/v1/{op}", handle)
    a.router.add_get("/local/state", local_state)
    a.router.add_get("/local/timings", local_timings)
    a.router.add_post("/local/unload", local_unload)
    a.router.add_post("/local/quit", local_quit)
    a.router.add_get("/", lambda r: web.Response(text="Fixer Desktop"))
    return a


def _seal_configs_at_rest() -> None:
    """11-22: upgrade the persistent config files (pairing keys, macros, LoRA catalog) to DPAPI-sealed at rest, in
    place. Best-effort and idempotent: a file is sealed only after a verified round-trip, and one that can't be sealed
    is left EXACTLY as it was and logged — never crashing startup. A legacy plaintext file is still read fine until it
    migrates, so this can't break pairing. (keys.json carries the AES keys, so it must not sit in plaintext on disk.)"""
    for path in (KEYS, MACROS_FILE, COMFY_LORAS / "library.json", CIVITAI_TOKEN, ROOT / "hf_token"):
        try:
            if sec.migrate(path, json_only=path.suffix == ".json", lock=(path == KEYS)) == "sealed":   # 8: keys.json under its lock
                log.warning("sealed %s at rest", path.name)
        except Exception as e:                         # noqa: BLE001
            log.warning("could not seal %s (left as-is): %s", path.name, e.__class__.__name__)


def main():
    global FREE_AFTER_MIN, FREE_AFTER_SAVED
    _JOB.own()                                         # 11-18: first, before any child: every child dies with us
    lower_priority()
    FREE_AFTER_MIN = FREE_AFTER_SAVED = _load_free_after()   # 11-18: the owner's last "free my computer after" choice
    import shutil
    shutil.rmtree(COMFY_PRIVATE, ignore_errors=True)   # a crash or kill could have left a job's photo behind
    _seal_configs_at_rest()                            # 11-22: seal keys.json / macros.json / library.json at rest
    if not KEYS.exists():
        print(f"No phones paired yet. Run:  python pair.py \"My iPhone\"   (keys go in {KEYS})", file=sys.stderr)
    print(f"Fixer Desktop on {HOST}:{PORT}  models: {MODELS} (cap {CAP_GB:.0f} GB)", file=sys.stderr)
    code = 1
    try:
        web.run_app(app(), host=HOST, port=PORT, access_log=None, print=None)
        code = 0
    except BaseException:                              # noqa: BLE001 - shown here: the hard exit below skips it
        import traceback
        traceback.print_exc()
    finally:
        # 11-20: bounded like _quit_now: a cleanup stuck on a lock left a server alive but no longer listening, and
        # the tray (which only checks the process) never started a new one.
        watchdog = threading.Timer(QUIT_GRACE, lambda: _EXIT(code))
        watchdog.daemon = True
        watchdog.start()
        _shutdown_cleanup()                            # chat model, helper, ComfyUI (also empties its private folder)
        try:
            sys.stderr.flush()
            sys.stdout.flush()
        except Exception:                              # noqa: BLE001
            pass
        # 11-18: a plain return would join the executor's threads at exit, and one stuck in a model load never ends
        _EXIT(code)


if __name__ == "__main__":
    main()
