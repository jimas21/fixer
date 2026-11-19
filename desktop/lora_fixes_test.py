"""Checks for the add-on fixes: Qwen 2.1 filing and the re-sort, the pair-safe cap of 4, and refusing Wan 2.2 5B.
Fakes only: no network, no ComfyUI, add-ons live in a throwaway folder.

    set FIXER_COMFY=0 && python desktop/lora_fixes_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import threading
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="fixer_lora_fixes_test_")
os.environ["FIXER_HOME"] = TMP
os.environ["FIXER_COMFY"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixer_desktop as fd  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


def st_file(path: Path, header: dict):
    raw = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * 8)


H21 = {"__metadata__": {"modelspec.architecture": "qwen-image-2.1/lora"}, "x": {}}
fd.COMFY_LORAS = Path(TMP) / "loras"
fd.COMFY_LORAS.mkdir()


def lib_with(entries: dict):
    for fn, e in entries.items():
        st_file(fd.COMFY_LORAS / fn, e.pop("_header", {}))
    base = {"name": "n", "family": "wan22", "triggers": [], "strength": 1.0, "always_on": False, "pair": None,
            "role": None, "source": "", "size": 1}
    fd._lora_lib_write({fn: {**base, **e} for fn, e in entries.items()})


try:
    # fix 1: family
    check(fd._resolve_family("Qwen", None)[0] == "qwen-edit", "plain Qwen label, no header -> qwen-edit")
    check(fd._resolve_family("Qwen 2.1", None)[0] == "qwen21", "label with 2.1 -> qwen21")
    check(fd._resolve_family("Qwen", H21)[0] == "qwen21", "header says 2.1 -> qwen21")
    check(fd._resolve_family("Qwen", {"a": {}})[0] == "qwen-edit", "header silent -> qwen-edit")

    # fix 1: re-sort
    lib_with({"a.safetensors": {"family": "qwen-edit", "_header": H21},
              "b.safetensors": {"family": "qwen-edit", "_header": {"k": {}}},
              "c.safetensors": {"family": "qwen-edit", "family_checked": True, "_header": H21},
              "d.safetensors": {"family": "sdxl", "_header": H21}})
    fd.job_lora_list({}, lambda m: None, threading.Event())
    lib = fd._lora_lib()
    check(lib["a.safetensors"]["family"] == "qwen21" and lib["a.safetensors"]["family_checked"], "re-sort moves 2.1 file")
    check(lib["b.safetensors"]["family"] == "qwen-edit" and lib["b.safetensors"]["family_checked"], "no evidence stays, marked")
    check(lib["c.safetensors"]["family"] == "qwen-edit", "already checked is left alone")
    check(lib["d.safetensors"]["family"] == "sdxl" and "family_checked" not in lib["d.safetensors"], "other family untouched")
    check(lib["a.safetensors"]["name"] == "n" and lib["a.safetensors"]["size"] == 1, "no other field changed")
    fd.LORA_LOCK.acquire()
    try:
        lib_with({"e.safetensors": {"family": "qwen-edit", "_header": H21}})
        fd._resort_qwen_loras()
        check(fd._lora_lib()["e.safetensors"]["family"] == "qwen-edit", "busy lock skips the pass")
    finally:
        fd.LORA_LOCK.release()

    # fix 1: job_lora_add keeps qwen21 over a picked qwen-edit
    plan = {"files": [{"name": "q.safetensors", "url": "https://x/q"}], "source": "link", "name": "Q", "base": "Qwen"}
    fd._parse_lora_link = lambda s: {}
    fd._plan_from_descriptor = lambda d: plan
    fd._stream_lora = lambda url, dest, emit, cancel: (dest.write_bytes(b"x"), (H21, "ab" * 32, 1))[1]
    fd.COMFY.stop = lambda *a, **k: None
    fd._lora_lib_write({})
    for picked, want in (("qwen-edit", "qwen21"), ("sdxl", "sdxl")):
        out = []
        fd.job_lora_add({"link": "x", "family": picked}, out.append, threading.Event())
        got = [m for m in out if m.get("added")][0]["added"][0]["family"]
        check(got == want, f"picked {picked} -> {got}, want {want}")

    # fix 9: 5B
    check("Wan Video 2.2 TI2V-5B" not in fd.LORA_CIVITAI_BASE["wan22"], "5B removed from search labels")
    fam, why = fd._resolve_family("Wan Video 2.2 TI2V-5B", None)
    check(fam is None and why, "5B label -> (None, reason)")
    check(fd._resolve_family("Wan Video 2.2 I2V-A14B", None)[0] == "wan22", "14B still wan22")
    called = []
    fd._stream_lora = lambda *a, **k: called.append(1)
    plan["base"] = "Wan Video 2.2 TI2V-5B"
    try:
        fd.job_lora_add({"link": "x", "family": "wan22"}, lambda m: None, threading.Event())
        check(False, "5B add should be refused")
    except fd.UserFacing:
        check(not called, "5B refused before any download")

    # fix 6: cap
    ent = {}
    for i in range(3):
        ent[f"s{i}.safetensors"] = {"family": "wan22"}
    ent["p1h.safetensors"] = {"family": "wan22", "pair": "P", "role": "high"}
    ent["p1l.safetensors"] = {"family": "wan22", "pair": "P", "role": "low"}
    ent["s9.safetensors"] = {"family": "wan22"}
    lib_with(ent)
    # s0, s1, pair (weight 2) -> 4 total; s2 and s9 left out
    loras, _ = fd._job_loras(["s0.safetensors", "s1.safetensors", "p1h.safetensors", "s2.safetensors"], "wan22")
    files = [x for t in loras for x in t[:2]]
    check(len(loras) == 3, f"three items kept: {loras}")
    check(files.count("p1h.safetensors") == 1 and files.count("p1l.safetensors") == 1, "pair kept whole")
    check(not any("s2" in f for f in files), "fifth weight left out")
    # a pair that does not fit is left out whole, and a later single still gets in
    loras, _ = fd._job_loras(["s0.safetensors", "s1.safetensors", "s2.safetensors", "p1h.safetensors", "s9.safetensors"],
                             "wan22")
    files = [x for t in loras for x in t[:2]]
    check("p1h.safetensors" not in files and "p1l.safetensors" not in files, "pair never split")
    check("s9.safetensors" in files, "walk carries on after a skip")
    # non-pair cap of 4 for a picture family
    lib_with({f"f{i}.safetensors": {"family": "sdxl"} for i in range(6)})
    loras, _ = fd._job_loras([f"f{i}.safetensors" for i in range(6)], "sdxl")
    check([f for f, _ in loras] == [f"f{i}.safetensors" for i in range(4)], "singles capped at 4 in order")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"{CHECKS} checks, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
