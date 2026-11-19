"""Checks for the phone's per-session Explicit switch (11-19): a job sent with "safe": true uses nothing explicit.
Fakes only: no network, no ComfyUI.

    set FIXER_COMFY=0 && python desktop/explicit_safe_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="fixer_explicit_safe_test_")
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


try:
    words = "a nude woman on a beach"

    # Old phones (no "safe") keep the old behaviour, untouched.
    old = {"prompt": words, "explicit": True}
    check(fd._safe_payload("image", old) is old, "no safe flag: payload untouched")
    check(fd._safe_payload("image", {"safe": False, "explicit": True})["explicit"] is True, "safe false: untouched")

    # Chat pictures and edits.
    p = {"prompt": words, "explicit": True, "safe": True}
    s = fd._safe_payload("image", p)
    check(s["explicit"] is False, "image: explicit forced false")
    check(p["explicit"] is True, "the original payload is not changed")

    # Videos.
    s = fd._safe_payload("video", {"prompt": words, "explicit": "on", "safe": True})
    check(s["explicit"] == "off", "video: explicit forced off")

    # Make runs: the add-on menu and the explicit editor.
    s = fd._safe_payload("comfy_run", {"recipe": "qwen21", "safe": True,
                                       "values": {"explicit": "On", "editor": "qwen-explicit", "brain_explicit": True}})
    check(s["values"]["explicit"] == "Off", "make: add-on menu forced Off")
    check(s["values"]["editor"] == "qwen", "make: explicit editor swapped for the everyday one")
    check("brain_explicit" not in s["values"], "make: brain flag dropped")
    s = fd._safe_payload("comfy_run", {"recipe": "juggernaut", "safe": True, "values": {"editor": "qwen21"}})
    check(s["values"]["editor"] == "qwen21", "make: other editors kept")

    # The words alone never turn explicit on inside a safe job.
    check(fd._is_explicit(words), "words are explicit normally")
    fd._TL.safe = True
    try:
        check(not fd._is_explicit(words), "safe job: words never count as explicit")
        check(fd._explicit_video(words, "auto") == 0.0, "safe job: no video add-on on auto")
        check(fd._qwen21_explicit("Off", True, None) is None, "safe job: no 2.1 add-on when Off")
    finally:
        fd._TL.safe = False
    check(fd._is_explicit(words), "after the job: words count again")

    # Add-ons (11-19): the 18+ mark, and safe jobs leave marked ones out (asked for or always on).
    check(fd._lora_is_explicit("x.safetensors", {"name": "Plain style", "explicit": True}), "own mark: 18+")
    check(not fd._lora_is_explicit("nsfw_pose.safetensors", {"name": "NSFW pose", "explicit": False}),
          "own mark beats the name")
    check(fd._lora_is_explicit("y.safetensors", {"name": "Film look", "nsfw": True}), "Civitai NSFW flag: 18+")
    check(fd._lora_is_explicit("nsfw_pose-ab12.safetensors", {"name": "pose"}), "explicit word in the file name")
    check(not fd._lora_is_explicit("film_grain.safetensors", {"name": "Film grain", "triggers": ["grainy"]}),
          "ordinary add-on: not 18+")
    lib_rows = [{"file": "a.safetensors", "family": "sdxl", "explicit": True, "always_on": True, "strength": 1.0},
                {"file": "b.safetensors", "family": "sdxl", "explicit": False, "always_on": True, "strength": 1.0},
                {"file": "c.safetensors", "family": "sdxl", "explicit": True, "always_on": False, "strength": 1.0}]
    orig_entries = fd._lora_entries
    fd._lora_entries = lambda: lib_rows
    try:
        got, _ = fd._job_loras(["c.safetensors"], "sdxl")
        names = sorted(x[0] for x in got)
        check(names == ["a.safetensors", "b.safetensors", "c.safetensors"], f"not safe: all add-ons used ({names})")
        fd._TL.safe = True
        try:
            got, _ = fd._job_loras(["c.safetensors"], "sdxl")
            names = sorted(x[0] for x in got)
            check(names == ["b.safetensors"], f"safe: only the ordinary add-on ({names})")
        finally:
            fd._TL.safe = False
        # Everyday models are always safe, 18+ on or off: Qwen-Image 2.1 and the plain editor.
        rows21 = [{"file": "x.safetensors", "family": "qwen21", "explicit": True, "always_on": True, "strength": 1.0},
                  {"file": "y.safetensors", "family": "qwen21", "explicit": False, "always_on": True, "strength": 1.0}]
        fd._lora_entries = lambda: rows21
        got, _ = fd._job_loras(None, "qwen21")
        check([x[0] for x in got] == ["y.safetensors"], "Qwen-Image 2.1: never an 18+ add-on, even with 18+ on")
        rows_ed = [{"file": "e.safetensors", "family": "qwen-edit", "explicit": True, "always_on": True,
                    "strength": 1.0}]
        fd._lora_entries = lambda: rows_ed
        check(fd._job_loras(None, "qwen-edit", allow_explicit=False)[0] == [], "plain editor: never an 18+ add-on")
        check(len(fd._job_loras(None, "qwen-edit", allow_explicit=True)[0]) == 1, "explicit editor: 18+ add-ons ok")
    finally:
        fd._lora_entries = orig_entries
    check(fd._qwen21_explicit("On", True, None) is None, "Qwen-Image 2.1: no explicit add-on even when asked")
    check(fd._adult_models() == {"chroma", "juggernaut"}, "the 18+ models are Chroma and Juggernaut")

    # The chat's Auto model list: safe never takes the explicit list.
    seen = []
    orig = fd._first_ready
    fd._first_ready = lambda ids: seen.append(list(ids)) or None
    try:
        a = fd._auto_models()
        fd._picture_recipe({"prompt": words, "explicit": True, "safe": True})
        check(seen[-1] == ["qwen21"], f"safe chat picture: only the everyday model ({seen[-1]})")
        fd._picture_recipe({"prompt": "a cat", "safe": True, "model_choice": "chroma"})
        check(seen[-1] == ["qwen21"], "safe: a chosen 18+ model is passed over for the everyday one")
        fd._picture_recipe({"prompt": words, "explicit": True})
        check(seen[-1] == a["picture_explicit"], "no safe flag: the explicit list as before")
    finally:
        fd._first_ready = orig
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"{CHECKS} checks, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
