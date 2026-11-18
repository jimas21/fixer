"""Pure-Python checks of the look-up tool and the identity/personality swap (11-17). Fakes only: no GPU, no
ComfyUI, no llama-server, no network, nothing written under the real data folder.

    set FIXER_COMFY=0 && python desktop\\lookup_test.py        (any Python with aiohttp + cryptography)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="fixer_lookup_test_")
os.environ["FIXER_HOME"] = TMP
os.environ["FIXER_COMFY"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixer_desktop as fd  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(cond, msg: str):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


def run_gate(chunks, allow=True):
    out: list[dict] = []
    g = fd._LookupGate(out.append, allow)
    for c in chunks:
        g.frame(c if isinstance(c, dict) else {"t": "text", "v": c})
    return g, out, g.finish()


def texts(out):
    return [f["v"] for f in out if f.get("t") == "text"]


def case_gate():
    g, out, req = run_gate(["Hel", "lo ", "there"])
    check(req is None and texts(out) == ["Hel", "lo ", "there"], "plain text streams in the same pieces")
    g, out, req = run_gate(['{"tool": "lo', 'okup", "what": ["models", ', '"phone"]}'])
    check(req == {"what": ["models", "phone"]} and texts(out) == [], "a look-up is held back and parsed")
    g, out, req = run_gate(['  { "tool" :', ' "lookup", "what": "pc"}'])
    check(req == {"what": ["computer"]} and texts(out) == [], "spaced JSON and a string topic")
    photo = '{"say": "On it", "actions": []}'
    g, out, req = run_gate(['{"say": "On it", ', '"actions": []}'])
    check(req is None and "".join(texts(out)) == photo, "photo JSON passes through unchanged")
    check(out and out[0]["v"].startswith('{"say"'), "photo JSON is released as soon as it can't be a look-up")
    g, out, req = run_gate([{"t": "thought", "v": "hmm"}, '{"tool": "lookup", "what": ["jobs"]}'])
    check(req == {"what": ["jobs"]} and out == [{"t": "thought", "v": "hmm"}], "thought frames pass while text is held")
    q = '{"tool": "queue_cancel", "ids": []}'
    g, out, req = run_gate([q])
    check(req is None and "".join(texts(out)) == q, "queue JSON passes through")
    g, out, req = run_gate(["{"])
    check(req is None and texts(out) == ["{"], "a lone brace is released at the end")
    g, out, req = run_gate(['{"tool": "lookup", "what": ["models"]}'], allow=False)
    check(req is None and texts(out) == [fd.LOOKUP_FAIL_TEXT], "no look-ups left: a plain sentence, never JSON")
    g, out, req = run_gate(['{"tool": "lookup", "what": ['])
    check(req is None and texts(out) == [fd.LOOKUP_FAIL_TEXT], "truncated look-up JSON is not shown")
    g, out, req = run_gate(["\n\n", "Sure"])
    check(req is None and "".join(texts(out)).strip() == "Sure", "leading blank lines don't confuse it")
    check(fd._lookup_topics([]) == ["models", "computer", "phone", "jobs"], "no topic means all of them")
    check(fd._lookup_topics(["iPhone", "GPU", "gpu", "zzz"]) == ["phone", "computer"],
          "topic words are mapped and deduplicated")


def case_rounds():
    calls: list[list[dict]] = []
    script: list[list] = []
    orig_once, orig_facts = fd._chat_llama_once, fd.lookup_facts

    def fake_once(p, emit, cancel, port):
        calls.append(list(p["messages"]))
        for c in script.pop(0):
            emit(c if isinstance(c, dict) else {"t": "text", "v": c})

    def fake_facts(topics, p):
        return "FACTS " + ",".join(topics)

    fd._chat_llama_once, fd.lookup_facts = fake_once, fake_facts
    try:
        base = {"messages": [{"role": "user", "text": "which models do you use?"}]}
        out: list[dict] = []
        script[:] = [['{"tool": "lookup", ', '"what": ["models"]}'], ["You run ", "Qwen."]]
        calls.clear()
        fd._chat_llama(base, out.append, threading.Event(), 1)
        check(len(calls) == 2, "one look-up means two generations")
        check(texts(out) == ["You run ", "Qwen."], "only the final answer reaches the phone")
        check(any(f.get("t") == "status" for f in out), "a plain status shows while it looks")
        m = calls[1]
        check(len(m) == 3 and m[1]["role"] == "assistant" and "lookup" in m[1]["text"]
              and m[2]["role"] == "user" and "FACTS models" in m[2]["text"], "facts are appended after its request")
        check(len(base["messages"]) == 1, "the caller's messages are not changed")
        out.clear()
        script[:] = [["Hi."]]
        calls.clear()
        fd._chat_llama(base, out.append, threading.Event(), 1)
        check(len(calls) == 1 and texts(out) == ["Hi."], "an ordinary answer is a single generation")
        out.clear()
        photo = '{"say": "Done", "actions": [{"tool": "upscale"}]}'
        script[:] = [[photo]]
        fd._chat_llama(base, out.append, threading.Event(), 1)
        check(texts(out) == [photo], "photo actions pass through")
        out.clear()
        script[:] = [['{"tool": "lookup", "what": ["models"]}'] for _ in range(3)] + [["unused"]]
        calls.clear()
        fd._chat_llama(base, out.append, threading.Event(), 1)
        check(len(calls) == 3, "at most two look-up rounds (three generations)")
        check(texts(out) == [fd.LOOKUP_FAIL_TEXT], "a third request ends in a plain sentence")
        out.clear()
        ev = threading.Event()
        ev.set()
        script[:] = [['{"tool": "lookup", "what": ["models"]}'], ["x"]]
        calls.clear()
        fd._chat_llama(base, out.append, ev, 1)
        check(len(calls) == 1, "a cancelled chat does not look anything up")
        out.clear()

        def boom(t, p):
            raise RuntimeError("x")

        fd.lookup_facts = boom
        script[:] = [['{"tool": "lookup", "what": ["computer"]}'], ["Couldn't read it."]]
        calls.clear()
        fd._chat_llama(base, out.append, threading.Event(), 1)
        check(len(calls) == 2 and "failed" in calls[1][-1]["text"], "a failed look-up tells the brain not to guess")
    finally:
        fd._chat_llama_once, fd.lookup_facts = orig_once, orig_facts


def case_personality():
    cfg = Path(TMP) / "phone_config.json"
    fd.PHONE_CONFIG = cfg
    cfg.unlink(missing_ok=True)
    ident = fd.ABOUT_IDENTITY_DEFAULT
    old_about = "About you (facts about where you run):\n- You are running on the owner's own computer.\n- Memory.\n"
    new_about = "About you (only when asked who you are):\n- Fixer.\n"
    tail = "Adult topics are fine."
    q = [{"role": "user", "text": "who are you"}]

    def swap(instr):
        return fd._without_meta({"instructions": instr, "messages": q})["instructions"]

    for label, about in (("build 51", old_about), ("next build", new_about)):
        r = swap(fd._PREVIOUS_DEFAULT_PERSONALITY + "\n\n" + about + "\n" + tail)
        check(r.startswith(fd.NEW_DEFAULT_PERSONALITY) and ident in r and "running on the owner" not in r
              and r.endswith(tail), f"{label}: default personality and about block are swapped")
    r = swap(fd._OLD_DEFAULT_PERSONALITY_START + " Old text.\n\n" + old_about + "\n" + tail)
    check(r.startswith(fd.NEW_DEFAULT_PERSONALITY) and "Old text" not in r and ident in r,
          "the first shipped default is swapped too")
    own = "Be terse. Call me boss."
    r = swap(own + "\n\n" + old_about + "\n" + tail)
    check(r.startswith(own) and fd.NEW_DEFAULT_PERSONALITY not in r and ident in r,
          "his own personality is never touched")
    r = swap(own + "\n\n" + tail)
    check(r.startswith(own) and r.endswith(ident), "no about block: the identity is added")
    mid = own + " " + fd._PREVIOUS_DEFAULT_PERSONALITY
    check(swap(mid).startswith(mid), "a default pasted after his own words is not swapped")
    cfg.write_text(json.dumps({"default_personality": "LIVE PERSONA", "about_identity": "LIVE WHO"}), encoding="utf-8")
    r = swap(fd.NEW_DEFAULT_PERSONALITY + "\n\n" + new_about + "\n" + tail)
    check(r.startswith("LIVE PERSONA") and "LIVE WHO" in r and ident not in r, "phone_config keys are live")
    check(swap(fd._PREVIOUS_DEFAULT_PERSONALITY).startswith("LIVE PERSONA"), "every shipped default goes to the live one")
    cfg.write_text("not json", encoding="utf-8")
    check(swap(fd._PREVIOUS_DEFAULT_PERSONALITY).startswith(fd.NEW_DEFAULT_PERSONALITY),
          "a broken config falls back to the code text")
    for t in (fd.NEW_DEFAULT_PERSONALITY, ident):
        check(not any(w in t.lower() for w in ("encrypted", "home computer", "rtx", "cloud ai")),
              "the texts say nothing about links or hardware as a matter of course")
    cfg.unlink(missing_ok=True)
    check(fd.LOOKUP_TOOLS_DEFAULT.count('"tool": "lookup"') == 1, "lookup tool text names the tool once")


def case_facts():
    check("doesn't send phone details" in fd._phone_facts(None) and "next app update" in fd._phone_facts({}),
          "an old phone build is told it needs the update")
    t = fd._phone_facts({"model": "iPhone17,1", "ios": "18.2", "battery": 0.55, "battery_state": "charging",
                         "storage_free_gb": 40.2, "extra": "x"})
    check("iPhone17,1" in t and "55%" in t and "charging" in t and "extra: x" in t,
          "phone facts list everything it sent")
    saved = fd._run_text
    fd._run_text = lambda args, timeout=8: ""
    try:
        m = fd.lookup_facts(["models"], {})
        check("CHAT BRAIN" in m and "PICTURE AND EDIT MODELS" in m and "LOADED RIGHT NOW" in m and "VIDEO" in m,
              "models facts cover brain, pictures, video and what is loaded")
        check("Z-Image Turbo" in m and "Chroma1-HD" in m and "Qwen-Image-Edit" in m, "every recipe is named")
        c = fd.lookup_facts(["computer"], {})
        check("graphics card" in c and "memory:" in c and "Windows" in c and "llama.cpp" in c and "ComfyUI" in c,
              "computer facts cover card, memory, Windows and engine versions")
        j = fd.lookup_facts(["jobs"], {"_queue": []})
        check("nothing is running" in j, "an empty line is said plainly")
        j = fd.lookup_facts(["jobs"], {"_queue": [{"id": "ab12", "kind": "image", "state": "running",
                                                   "label": "a cat", "progress": 0.5}]})
        check("ab12" in j and "50%" in j, "jobs facts reuse the live queue note")
        both = fd.lookup_facts(["phone", "jobs"], {"device": {"model": "X"}, "_queue": []})
        check("PHONE" in both and "JOBS" in both, "several topics in one look-up")
        fd.TIMINGS.clear()
        fd.TIMINGS.append({"kind": "picture", "model": "Qwen-Image 2.1 Turbo", "recipe": "qwen21", "explicit": False,
                           "mp": 1.05, "steps": 8, "count": 1, "load_s": 2.1, "draw_s": 6.8, "total_s": 9.4,
                           "loaded": False, "ok": True, "at": 0.0})
        j = fd.lookup_facts(["jobs"], {"_queue": []})
        check("RECENT FINISHED JOBS" in j and "Qwen-Image 2.1 Turbo" in j and "total 9.4 s" in j
              and "had to load the model: no" in j, "jobs facts list the recent finished jobs")
        s = fd._measured_speed_facts()
        check(any("Qwen-Image 2.1 Turbo, new picture" in x and "9.4 s on average over 1" in x for x in s),
              "measured speed lines name the model and the average")
        fd.TIMINGS.clear()
        check(fd._measured_speed_facts()[-1] == "- none yet this session", "no jobs yet is said plainly")
    finally:
        fd._run_text = saved
    orig = fd._computer_facts
    fd._computer_facts = lambda: 1 / 0
    try:
        check("couldn't be read" in fd.lookup_facts(["computer"], {}), "a topic that fails says so")
    finally:
        fd._computer_facts = orig


if __name__ == "__main__":
    try:
        for name, case in list(globals().items()):
            if name.startswith("case_"):
                case()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{CHECKS} checks, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
