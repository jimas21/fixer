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

_REAL_STREAM, _REAL_PARSE, _REAL_PLAN = fd._stream_lora, fd._parse_lora_link, fd._plan_from_descriptor

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

    # 11-22: the macro AI's patches. Empty results are left out (the phone's merge overwrites any key that is present),
    # a bad strength does not abort, and a safe session uses the app's own 18+ test (owner's mark, role names)
    lib_with({"ok.safetensors": {"family": "sdxl", "nsfw": False, "name": "plain"},
              "hot.safetensors": {"family": "sdxl", "explicit": True},
              "newciv.safetensors": {"family": "sdxl", "source": "civitai"},
              "seen.safetensors": {"family": "sdxl", "nsfw": True, "explicit": False}})
    fd._macros_write({"roles": {"realism": {"file": "ok.safetensors", "strength": 1.0, "enabled": True},
                                "undress-helper": {"file": "ok.safetensors", "strength": 1.0, "enabled": True},
                                "spicy": {"file": "hot.safetensors", "strength": 1.0, "enabled": True}}, "macros": []})
    check(fd._clean_patch({"prompt_add": None, "negative_add": None, "settings": {}, "loras": []}, False) == {},
          "null / empty fields are left out of a patch")
    check(fd._clean_patch({"loras": [{"file": "invented.safetensors"}]}, False) == {}, "all-invalid add-ons: no loras key")
    ap = fd._clean_patch({"loras": [{"file": "ok.safetensors", "strength": None},
                                    {"file": "seen.safetensors", "strength": "0.5-0.8"}]}, False)
    check(ap == {"loras": [{"file": "ok.safetensors", "strength": 1.0}, {"file": "seen.safetensors", "strength": 1.0}]},
          "a null or range strength falls back to 1.0 instead of raising")
    ap = fd._clean_patch({"loras": [{"file": "ok.safetensors"}, {"file": "hot.safetensors"},
                                    {"file": "newciv.safetensors"}, {"file": "seen.safetensors"},
                                    {"role": "realism"}, {"role": "undress-helper"}, {"role": "spicy"}]}, True)
    check([x.get("file") or x.get("role") for x in ap["loras"]] == ["ok.safetensors", "seen.safetensors", "realism"],
          "safe: explicit files and explicit roles are dropped (owner's mark wins over Civitai's flag)")
    ap = fd._clean_patch({"loras": [{"file": "hot.safetensors"}, {"role": "undress-helper"}]}, False)
    check(len(ap["loras"]) == 2, "not safe: nothing about the add-ons changes")
    cat = "\n".join(fd._lora_catalog_for_ai(True))
    check("ok.safetensors" in cat and "seen.safetensors" in cat and "hot.safetensors" not in cat
          and "newciv.safetensors" not in cat, "the safe catalog uses the same 18+ test")
    check("hot.safetensors" in "\n".join(fd._lora_catalog_for_ai(False)), "the catalog is whole when not safe")
    for raw in ("not json at all", '{"suggestions": [{"title": "t", "detail": "d", "apply": '):
        fd._brain_chat = lambda *a, _raw=raw, **k: _raw
        for job in (fd.job_analyze_macro, fd.job_recommend_macros):
            try:
                job({"macro": {"name": "m"}}, lambda o: None, threading.Event())
                check(False, "an unreadable reply is an error, not an empty list")
            except fd.UserFacing:
                check(True, "")
    fd._brain_chat = lambda *a, **k: '{"suggestions": [{"title": "swap", "apply": {"loras": [{"file": "x.safetensors"}]}}]}'
    got = []
    fd.job_analyze_macro({"macro": {"name": "m"}}, got.append, threading.Event())
    check(got and got[-1]["suggestions"] == [], "a suggestion whose change was all invalid is dropped")
    # 11-22 (minor): advice-only suggestions ("apply": {} or no apply) are kept; a non-empty apply that cleaned to
    # nothing (an invented add-on, a null text) is still dropped
    fd._brain_chat = lambda *a, **k: json.dumps({"suggestions": [
        {"title": "tip one", "detail": "d", "apply": {}}, {"title": "tip two", "detail": "d"},
        {"title": "invented", "apply": {"loras": [{"file": "x.safetensors"}]}},
        {"title": "null text", "apply": {"prompt_add": None}},
        {"title": "real", "apply": {"negative_add": "blurry"}}]})
    got = []
    fd.job_analyze_macro({"macro": {"name": "m"}}, got.append, threading.Event())
    sg = got[-1]["suggestions"]
    check([s["title"] for s in sg] == ["tip one", "tip two", "real"], f"advice-only kept, emptied ones dropped: {sg}")
    check("apply" not in sg[0] and "apply" not in sg[1] and sg[2]["apply"] == {"negative_add": "blurry"},
          "advice-only suggestions carry no apply key")

    # 11-22 (21): an 'apply' patch analysed WITH the macro gets a saved macro's family / cap / budget rules
    _real_ready = fd._recipe_ready
    fd._recipe_ready = lambda r: True
    try:
        lib_with({**{f"s{i}.safetensors": {"family": "sdxl"} for i in range(1, 7)},
                  "c1.safetensors": {"family": "chroma"}, "u1.safetensors": {"family": "unknown"}})
        files = lambda ap: [x.get("file") or x.get("role") for x in ap.get("loras", [])]  # noqa: E731
        patch = {"loras": [{"file": "s1.safetensors"}, {"file": "c1.safetensors"}, {"file": "u1.safetensors"}]}
        check(files(fd._clean_patch(patch, False)) == ["s1.safetensors", "c1.safetensors", "u1.safetensors"],
              "without context nothing is trimmed (as before)")
        check(files(fd._clean_patch(patch, False, {"model": "juggernaut"})) == ["s1.safetensors", "u1.safetensors"],
              "a wrong-family add-on is dropped for the macro's model (unknown family passes)")
        check(fd._clean_patch({"loras": [{"file": "c1.safetensors"}]}, False, {"model": "juggernaut"}) == {},
              "every add-on the wrong family: no loras key (never [])")
        check(files(fd._clean_patch({**patch, "model": "chroma"}, False, {"model": "juggernaut"}))
              == ["c1.safetensors", "u1.safetensors"], "the patch's own model decides the family")
        check(files(fd._clean_patch({"loras": [{"role": "realism"}, {"file": "c1.safetensors"}]}, False,
                                    {"model": "juggernaut"})) == ["realism"], "a role add-on always fits")
        check(files(fd._clean_patch(patch, False, {"model": "not-a-model"})) == files(fd._clean_patch(patch, False)),
              "a macro with no known model filters nothing by family")
        cap = int(fd._tune("lora_cap", 4))
        many = {"loras": [{"file": f"s{i}.safetensors", "strength": 0.2} for i in range(1, 7)]}
        check(len(fd._clean_patch(many, False, {"model": "juggernaut"})["loras"]) == cap, f"more than the cap ({cap}) is trimmed")
        check(len(fd._clean_patch(many, False)["loras"]) == 6, "no context: no cap applied")
        hot = {"loras": [{"file": f"s{i}.safetensors", "strength": 1.0} for i in range(1, 4)]}
        got_s = [x["strength"] for x in fd._clean_patch(hot, False, {"model": "juggernaut"})["loras"]]
        check(all(s < 1.0 for s in got_s) and sum(got_s) <= float(fd._tune("macro_strength_budget", 1.6)) + 0.02,
              f"strengths over the budget are scaled down together: {got_s}")
        check([x["strength"] for x in fd._clean_patch(hot, False)["loras"]] == [1.0, 1.0, 1.0], "no context: not scaled")
        # a model switch alone: the macro's own add-ons stay (the phone's merge keeps them), so it must still fit them
        check(fd._clean_patch({"model": "chroma"}, False, {"model": "juggernaut", "loras": [{"file": "s1.safetensors"}]}) == {},
              "a switch to a family the macro's own add-ons don't fit is left out")
        check(fd._clean_patch({"model": "chroma"}, False, {"model": "juggernaut", "loras": [{"file": "c1.safetensors"},
                                                                                            {"role": "realism"}]})
              == {"model": "chroma"}, "a switch their add-ons fit is kept")
        check(fd._clean_patch({"model": "chroma"}, False) == {"model": "chroma"}, "no context: the switch is kept")
        # job_analyze_macro hands over the macro it is reviewing
        fd._brain_chat = lambda *a, **k: json.dumps({"suggestions": [{"title": "swap", "apply": patch}]})
        got = []
        fd.job_analyze_macro({"macro": {"name": "m", "model": "juggernaut"}}, got.append, threading.Event())
        check(files(got[-1]["suggestions"][0]["apply"]) == ["s1.safetensors", "u1.safetensors"],
              "job_analyze_macro trims the patch for the macro's model")
        # and a normal macro save still trims the same way (the shared helper)
        m = fd._sanitize_macro({"name": "n", "prompt_add": "p", "model": "juggernaut", "loras": patch["loras"]}, False)
        check(files(m) == ["s1.safetensors", "u1.safetensors"], "the saved-macro path is unchanged")
    finally:
        fd._recipe_ready = _real_ready
    # 11-22: a phone-supplied add-on name can never leave the add-on folder (lora_set / lora_remove / macros)
    decoy = Path(TMP) / "decoy.safetensors"
    decoy.write_bytes(b"keep me")
    lib_with({"real.safetensors": {"family": "sdxl"}})
    for bad in ("..\\decoy.safetensors", "../decoy.safetensors", str(decoy), "sub\\x.safetensors", "C:x.safetensors",
                "library.json", "..", "", "real.safetensors ", "a\0b.safetensors"):
        check(fd._lora_path(bad) is None, f"not a plain add-on name: {bad!r}")
        for job in (fd.job_lora_remove, fd.job_lora_set):
            try:
                job({"file": bad, "strength": 1.0}, lambda o: None, threading.Event())
                check(False, f"{job.__name__} must refuse {bad!r}")
            except fd.UserFacing:
                check(True, "")
    check(decoy.is_file() and decoy.read_bytes() == b"keep me", "a file outside the folder is never touched")
    check((fd.COMFY_LORAS / "library.json").is_file(), "the library file is not deletable by name")
    check(fd._lora_path("real.safetensors") == fd.COMFY_LORAS / "real.safetensors", "a plain name is accepted")

    # 34: a strength of 0 stays 0 (add-on off); a missing / bad one is 1.0
    for sent, want in ((0, 0.0), (0.0, 0.0), (0.5, 0.5), (None, 1.0), ("x", 1.0), (9, 2.0)):
        fd.job_lora_set({"file": "real.safetensors", "strength": sent}, lambda o: None, threading.Event())
        check(fd._lora_lib()["real.safetensors"]["strength"] == want, f"strength {sent!r} -> {want}")

    # 68 / 32: saving macros keeps an add-on that is not installed (in a macro the phone did not change) and keys
    # the phone does not edit, but still drops a newly added one that is not installed
    fd._macros_write({"roles": {}, "macros": [
        {"id": "keep", "name": "Keep", "loras": [{"file": "gone.safetensors", "strength": 0.7}], "cfg_drop": 3},
        {"id": "other", "name": "Other", "loras": []}]})
    out = []
    fd.job_macros_set({"roles": {}, "macros": [
        {"id": "keep", "name": "Keep", "loras": [{"file": "gone.safetensors", "strength": 0.7}], "pinned": True},
        {"id": "other", "name": "Other", "loras": [{"file": "never.safetensors"}, {"file": "real.safetensors"}]}]},
        out.append, threading.Event())
    ms = {m["id"]: m for m in fd._macros()["macros"]}
    check(ms["keep"]["loras"] == [{"file": "gone.safetensors", "strength": 0.7}] and ms["keep"]["pinned"],
          "an uninstalled add-on the macro already had survives a save")
    check(ms["keep"].get("cfg_drop") == 3, "a key the phone does not edit is carried through")
    check([x["file"] for x in ms["other"]["loras"]] == ["real.safetensors"], "a newly added uninstalled add-on is dropped")

    # 30 / 36: a cut-off download and a non-object header are refused; a whole file is accepted
    class FakeResp:
        def __init__(self, body, length):
            self.headers = {"Content-Length": str(length)}
            self._b, self._i = body, 0

        def read(self, n):
            c = self._b[self._i:self._i + n]
            self._i += len(c)
            return c

        def close(self):
            self.closed = True

    def st_bytes(header, data_len):
        raw = json.dumps(header).encode()
        return struct.pack("<Q", len(raw)) + raw + b"\0" * data_len

    hdr = {"w": {"dtype": "F16", "shape": [10], "data_offsets": [0, 20]}}
    whole = st_bytes(hdr, 20)
    dest = Path(TMP) / "dl.part"
    fd._open_checked = lambda url: FakeResp(whole, len(whole))
    check(_REAL_STREAM("https://x/a", dest, lambda m: None, threading.Event())[2] == len(whole), "a whole file is accepted")
    fd._open_checked = lambda url: FakeResp(whole[:-6], len(whole))
    for label, resp in (("short of Content-Length", FakeResp(whole[:-6], len(whole))),
                        ("short of its own tensor offsets", FakeResp(whole[:-6], len(whole) - 6)),
                        ("null header", FakeResp(st_bytes(None, 4), len(st_bytes(None, 4)))),
                        ("list header", FakeResp(st_bytes([1, 2], 4), len(st_bytes([1, 2], 4))))):
        fd._open_checked = lambda url, _r=resp: _r
        try:
            _REAL_STREAM("https://x/a", dest, lambda m: None, threading.Event())
            check(False, f"{label} should be refused")
        except fd.UserFacing:
            check(True, "")
    big = FakeResp(b"", 3 * 1024 ** 3)
    fd._open_checked = lambda url: big
    try:
        _REAL_STREAM("https://x/a", dest, lambda m: None, threading.Event())
        check(False, "oversized should be refused")
    except fd.UserFacing:
        check(getattr(big, "closed", False), "an oversized Content-Length closes the connection")

    # 37: a Hugging Face link keeps its revision
    d = _REAL_PARSE("https://huggingface.co/a/b/blob/v2/dir/lora.safetensors")
    check(d.get("rev") == "v2" and d["file"] == "dir/lora.safetensors", f"revision carried: {d}")
    check("/resolve/v2/dir/lora.safetensors" in _REAL_PLAN(d)["files"][0]["url"], "download URL uses the revision")
    check("/resolve/main/" in _REAL_PLAN({"kind": "hf_file", "repo": "a/b", "file": "x.safetensors"})["files"][0]["url"],
          "no revision -> main")

    # ---- 11-22 (8, keys-get, 31, 39): keys.json lock, an unreadable keys.json, library races, the Civitai key sealed
    import logging
    import subprocess
    import time as _time
    import fixer_secure as sec
    import pair as pairmod

    LOGS: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            LOGS.append(record.getMessage())

    _grab = _Grab()
    logging.getLogger("fixer").addHandler(_grab)
    here = str(Path(__file__).resolve().parent)
    CHILD = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport fixer_secure as sec\npath, tag = sys.argv[2], sys.argv[3]\n"
             "for i in range(12):\n    sec.update_json(path, lambda d: d.setdefault('phones', {}).__setitem__(f'{tag}-{i}', {'k': i}))\n")
    HOLD = ("import sys, time\nsys.path.insert(0, sys.argv[1])\nimport fixer_secure as sec\n"
            "with sec.file_lock(sys.argv[2]):\n    print('held', flush=True)\n    time.sleep(float(sys.argv[3]))\n")

    # 8: five processes each add 12 pairings to one sealed file: none may be lost
    kp = Path(TMP) / "shared_keys.json"
    kids = [subprocess.Popen([sys.executable, "-I", "-c", CHILD, here, str(kp), f"p{n}"], stderr=subprocess.PIPE)
            for n in range(5)]
    errs = [k.communicate(timeout=120)[1].decode() for k in kids]
    check(all(k.returncode == 0 for k in kids), f"child processes finished cleanly: {errs}")
    got = sec.load_json_object(kp).get("phones", {})
    check(len(got) == 60, f"60 pairings from 5 processes all kept, got {len(got)}")
    check(sec.is_sealed(kp), "the shared file is sealed at rest")

    # 8: a held lock makes a second taker wait, then fail with a clear error; a killed holder leaves no stale lock
    h = subprocess.Popen([sys.executable, "-I", "-c", HOLD, here, str(kp), "30"], stdout=subprocess.PIPE)
    check(h.stdout.readline().strip() == b"held", "holder has the lock")
    try:
        with sec.file_lock(kp, timeout=0.4):
            check(False, "a second taker must not get the lock")
    except sec.LockTimeout as e:
        check("busy" in str(e), f"clear timeout error: {e}")
    h.kill()
    h.wait()
    try:
        with sec.file_lock(kp, timeout=5):
            check(True, "")
    except sec.LockTimeout:
        check(False, "a killed holder must not leave a stale lock")
    with sec.file_lock(kp):                              # nested use in one thread does not deadlock
        with sec.file_lock(kp, timeout=0.5):
            check(True, "")

    # 8: confirm_pairing and the expiry sweep go through the lock, keep every other pairing, stay sealed
    now = _time.time()
    sec.write_text(fd.KEYS, json.dumps({"phones": {
        "new": {"name": "n", "key": "a" * 43, "pair_by": now + 600},
        "old": {"name": "o", "key": "b" * 43, "pair_by": now - 5},
        "tmp": {"name": "t", "key": "c" * 43, "expires": now - 5},
        "kept": {"name": "k", "key": "d" * 43}}}))
    fd.confirm_pairing("new")
    ph = sec.load_json_object(fd.KEYS)["phones"]
    check("pair_by" not in ph["new"] and "paired_at" in ph["new"] and len(ph) == 4, "confirm keeps the other pairings")
    fd._drop_expired_pairings()
    ph = sec.load_json_object(fd.KEYS)["phones"]
    check(sorted(ph) == ["kept", "new"] and "pair_by" not in ph["new"], f"sweep drops only expired, keeps the confirmation: {sorted(ph)}")
    check(sec.is_sealed(fd.KEYS), "keys.json stays sealed after both writers")
    ks = fd.Keys(fd.KEYS)
    check(ks.get("kept") is not None and ks.get("gone") is None, "Keys.get reads the sealed file")

    # keys-get: a corrupt / truncated / unsealable keys.json is no keys, logged once, and never written over
    for label, blob in (("garbage", b"\x00\x01 not json"), ("truncated", b'{"phones": {"a": {"key": "'),
                        ("sealed junk", sec.MAGIC + b"not a dpapi blob"), ("array", b"[1, 2]")):
        fd.KEYS.write_bytes(blob)
        ks, LOGS[:] = fd.Keys(fd.KEYS), []
        try:
            r = (ks.get("kept"), ks.get("kept"), ks.pending("kept"))
        except Exception as e:                           # noqa: BLE001
            r = ("raised", e.__class__.__name__)
        check(r == (None, None, False), f"{label}: Keys.get gives no keys and does not raise: {r}")
        check(sum("keys.json can't be read" in m for m in LOGS) == 1, f"{label}: logged once, got {LOGS}")
        fd.confirm_pairing("kept")
        fd._drop_expired_pairings()
        check(fd.KEYS.read_bytes() == blob, f"{label}: the server never overwrites a file it couldn't read")
        try:
            pairmod.update(lambda d: d["phones"].__setitem__("x", {}))
            check(False, f"{label}: pair.update must refuse")
        except sec.ConfigUnreadable:
            check(fd.KEYS.read_bytes() == blob, f"{label}: pair.py refuses and leaves the file as it was")
        check(pairmod.load() == {"phones": {}}, f"{label}: pair.load() is tolerant")
        try:
            pairmod.load(strict=True)
            check(False, f"{label}: strict load must raise")
        except sec.ConfigUnreadable:
            check(True, "")
    # the file is fixed: Keys.get recovers on its new mtime
    sec.write_text(fd.KEYS, json.dumps({"phones": {"kept": {"name": "k", "key": "d" * 43}}}))
    os.utime(fd.KEYS, (1, 2_000_000_000))
    check(ks.get("kept") is not None, "Keys.get recovers when the file is fixed")
    # pair.py's own changes: new pairing, then remove, through the lock
    pid, link, q = pairmod.new_pairing("Test phone", url="http://192.0.2.1:8765", remote="")
    ph = sec.load_json_object(fd.KEYS)["phones"]
    check(pid in ph and "kept" in ph and "pair_by" in ph[pid] and sec.is_sealed(fd.KEYS), "new_pairing adds under the lock, sealed")
    pairmod.update(lambda d: d["phones"].pop(pid, None))
    check(pid not in sec.load_json_object(fd.KEYS)["phones"] and "kept" in sec.load_json_object(fd.KEYS)["phones"],
          "pair.update removes one phone only")

    # keys-get (review): a read that fails only for a moment (scanner, backup, brief DPAPI error) is never cached as
    # "no keys": it is retried at once, and again after a short wait, with no change to the file
    _real_load = sec.load_json_object
    _calls = {"n": 0, "fail_until": 0}

    def _flaky(path):
        _calls["n"] += 1
        if _calls["n"] <= _calls["fail_until"]:
            e_ = sec.ConfigUnreadable("keys.json can't be read (PermissionError), so it was left unchanged.")
            e_.read_failed = True
            raise e_
        return _real_load(path)

    sec.load_json_object = _flaky
    try:
        ks = fd.Keys(fd.KEYS)
        _calls.update(n=0, fail_until=1)                 # one failure, then fine: the in-call retry recovers it
        check(ks.get("kept") is not None and _calls["n"] == 2, f"one failed read is retried straight away (reads: {_calls['n']})")
        ks = fd.Keys(fd.KEYS)
        _calls.update(n=0, fail_until=3)                 # all three attempts fail
        check(ks.get("kept") is None and ks.mtime is None, "a read that kept failing is not remembered as this file's keys")
        check(ks.get("kept") is None and _calls["n"] == 3, "and it is not re-read on every request")
        ks.retry_at = 0.0                                # the short wait is over
        check(ks.get("kept") is not None, "the next try after the wait reads the same, unchanged file")
    finally:
        sec.load_json_object = _real_load

    # recovery from an unreadable keys.json: set aside (kept), then everything works again
    junk = sec.MAGIC + b"not a dpapi blob"
    fd.KEYS.write_bytes(junk)
    try:
        pairmod.new_pairing("Phone", url="http://192.0.2.1:8765", remote="")
        check(False, "new_pairing must refuse an unreadable keys.json")
    except sec.ConfigUnreadable as e:
        check("--reset keys" in pairmod.RESET_HINT and fd.KEYS.read_bytes() == junk, f"refused, file untouched: {e}")
    r = subprocess.run([sys.executable, str(Path(here) / "pair.py"), "--reset", "keys"], capture_output=True, text=True, cwd=TMP,
                       env={**os.environ, "FIXER_HOME": str(fd.KEYS.parent)}, timeout=60)
    kept = sorted(fd.KEYS.parent.glob("keys.json.unreadable-*"))
    check(r.returncode == 0 and "Moved keys.json aside" in r.stdout and not fd.KEYS.exists(),
          f"--reset keys moves it aside: {r.returncode} {r.stdout!r} {r.stderr[-200:]!r}")
    check(len(kept) == 1 and kept[0].read_bytes() == junk, "the unreadable file is kept under a new name, byte for byte")
    pid, link, q = pairmod.new_pairing("Phone", url="http://192.0.2.1:8765", remote="")
    check(pid in sec.load_json_object(fd.KEYS)["phones"], "pairing works again on the fresh file")
    check("can be read" in pairmod.reset_config("keys") and fd.KEYS.exists() and len(list(fd.KEYS.parent.glob("keys.json.unreadable-*"))) == 1,
          "a readable keys.json is never moved by --reset")
    fd.KEYS.unlink()
    check("nothing to reset" in pairmod.reset_config("keys"), "no file: nothing to reset")

    # 31: a library edit or removal made while an add-on downloads survives the add
    lib_with({"old.safetensors": {"family": "sdxl"}, "keep.safetensors": {"family": "sdxl", "strength": 1.0},
              "other.safetensors": {"family": "sdxl"}})
    plan2 = {"files": [{"name": "dl.safetensors", "url": "https://x/dl"}], "source": "link", "name": "DL", "base": "SDXL 1.0"}
    fd._parse_lora_link = lambda s_: {}
    fd._plan_from_descriptor = lambda d: plan2

    def slow_stream(url, dest, emit, cancel):
        fd.job_lora_remove({"file": "old.safetensors"}, lambda m: None, threading.Event())     # mid-download edits
        fd.job_lora_set({"file": "keep.safetensors", "strength": 0.4}, lambda m: None, threading.Event())
        dest.write_bytes(b"x")
        return ({"a": 1}, "cd" * 32, 1)

    fd._stream_lora = slow_stream
    fd.job_lora_add({"link": "x", "family": "sdxl"}, lambda m: None, threading.Event())
    lib = fd._lora_lib()
    check("old.safetensors" not in lib, "a removal made during the download is not undone by the add")
    check(lib["keep.safetensors"]["strength"] == 0.4, "a strength change made during the download is kept")
    check("other.safetensors" in lib and any(k.startswith("dl-") for k in lib), "the new add-on is merged in, others stay")

    # 31: concurrent edits from several threads are all applied
    lib_with({f"t{i}.safetensors": {"family": "sdxl"} for i in range(8)})
    ths = [threading.Thread(target=fd.job_lora_set, args=({"file": f"t{i}.safetensors", "strength": 0.1 * (i + 1)},
                                                         lambda m: None, threading.Event())) for i in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    lib = fd._lora_lib()
    check(all(abs(lib[f"t{i}.safetensors"]["strength"] - 0.1 * (i + 1)) < 1e-9 for i in range(8)), "8 simultaneous edits all land")

    # 31: _lora_lib() is {} for a missing / non-dict file; an unreadable one is refused by writers, never overwritten
    libf = fd.COMFY_LORAS / "library.json"
    libf.unlink()
    check(fd._lora_lib() == {}, "missing library -> {}")
    for blob in (b"[1, 2]", b"null"):
        libf.write_bytes(blob)
        check(fd._lora_lib() == {}, f"non-dict library {blob!r} -> {{}}")
    for blob in (b"{ truncated", sec.MAGIC + b"junk"):
        libf.write_bytes(blob)
        check(fd._lora_lib() == {}, "reading an unreadable library gives {}")
        gone_f = fd.COMFY_LORAS / "t0.safetensors"       # remove must refuse BEFORE it deletes the file
        try:
            fd.job_lora_remove({"file": "t0.safetensors"}, lambda m: None, threading.Event())
            check(False, "remove on an unreadable library should refuse")
        except fd.UserFacing:
            check(gone_f.is_file() and libf.read_bytes() == blob, "remove on an unreadable library deletes nothing")
        try:
            fd.job_lora_set({"file": "t0.safetensors", "strength": 1.0}, lambda m: None, threading.Event())
            check(False, "lora_set must refuse an unreadable library")
        except fd.UserFacing:
            check(libf.read_bytes() == blob, "the unreadable library is left exactly as it was")
    # recovery: the refusal names the step, and it moves the list aside (kept) so adds work again
    try:
        fd.job_lora_set({"file": "t0.safetensors", "strength": 1.0}, lambda m: None, threading.Event())
    except fd.UserFacing as e:
        check("pair.py --reset library" in str(e), f"the library refusal says how to recover: {e}")
    pairmod.LIBRARY = libf
    moved_msg = pairmod.reset_config("library")
    kept = list(libf.parent.glob("library.json.unreadable-*"))
    check(not libf.exists() and len(kept) == 1 and kept[0].read_bytes() == blob and "Moved library.json aside" in moved_msg,
          f"--reset library keeps the old list under a new name: {moved_msg}")
    check(fd._lora_lib_for_update() == {}, "writers see a fresh empty list afterwards")
    check("nothing to reset" in pairmod.reset_config("library"), "a second reset finds nothing")
    for k_ in kept:
        k_.unlink()
    libf.unlink(missing_ok=True)

    # 39: the Civitai key is sealed on disk, read back, never sent to the phone or logged; a legacy plaintext file
    # is still read and is sealed by the startup step
    SECRET = "civ-test-key-0123456789abcdef"
    LOGS[:] = []
    out = []
    fd.job_lora_key({"key": f"  {SECRET}  "}, out.append, threading.Event())
    raw = fd.CIVITAI_TOKEN.read_bytes()
    check(sec.is_sealed(fd.CIVITAI_TOKEN) and SECRET.encode() not in raw, "the key is sealed on disk, not plaintext")
    check(fd._civitai_token() == SECRET, "the key reads back")
    check(SECRET not in json.dumps(out) and out[-1].get("civitai_key") is True, "the key is never sent to the phone")
    out2 = []
    fd.job_lora_list({}, out2.append, threading.Event())
    check(SECRET not in json.dumps(out2, default=str) and out2[-1].get("civitai_key") is True, "lora_list only says a key exists")
    check(fd._lora_headers("https://civitai.com/api/x").get("Authorization") == f"Bearer {SECRET}", "sent to Civitai")
    check("Authorization" not in fd._lora_headers("https://example.com/x"), "not sent to another host")
    check(not any(SECRET in m for m in LOGS), "the key is not logged")
    fd.CIVITAI_TOKEN.write_text(SECRET + "\n", encoding="utf-8")        # an old plaintext install
    check(fd._civitai_token() == SECRET, "a legacy plaintext key is still read")
    fd._seal_configs_at_rest()
    check(sec.is_sealed(fd.CIVITAI_TOKEN) and SECRET.encode() not in fd.CIVITAI_TOKEN.read_bytes()
          and fd._civitai_token() == SECRET, "the startup step seals a legacy plaintext key")
    fd.CIVITAI_TOKEN.write_text("old-plaintext-key", encoding="utf-8")
    fd.job_lora_key({"key": "new-key-value"}, lambda m: None, threading.Event())
    check(sec.is_sealed(fd.CIVITAI_TOKEN) and fd._civitai_token() == "new-key-value", "the next write replaces plaintext with sealed")
    fd.job_lora_key({"key": ""}, lambda m: None, threading.Event())
    check(not fd.CIVITAI_TOKEN.exists() and fd._civitai_token() == "", "clearing the key removes the file")
    logging.getLogger("fixer").removeHandler(_grab)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"{CHECKS} checks, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
