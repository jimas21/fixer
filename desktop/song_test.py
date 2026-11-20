"""Song job (11-20): the writer's JSON handling and job_song's flow, with the chat model and ComfyUI stubbed out.
Run: python song_test.py"""
import asyncio
import base64
import json
import threading

import fixer_desktop as fd

fails = 0
checks = 0


def check(ok, what):
    global fails, checks
    checks += 1
    if not ok:
        fails += 1
        print("FAIL", what)


def writer_answer(text):
    def fake_chat(p, emit, cancel, port):
        fake_chat.last = p
        emit({"t": "text", "v": text})
    return fake_chat


fd._enhance_port = lambda: 1234
good = json.dumps({"title": "Rain Town", "tags": "indie pop, female vocal, warm", "lyrics": "[Verse 1]\nla",
                   "bpm": 400, "key": "A minor"})

# writer: fenced JSON, bpm clamp, key check, the prompt slot
fd._chat_llama = writer_answer("```json\n" + good + "\n```")
w = fd._write_song("a rainy day", "", False, 120, False, threading.Event())
check(w and w["title"] == "Rain Town" and w["bpm"] == 180 and w["key"] == "A minor", "writer parses and clamps")
check(fd._chat_llama.last["_slot"] == fd.PROMPT_SLOT, "writer uses the prompt writer's slot")
check("clean" not in fd._chat_llama.last["instructions"], "18+ on: no clean rule")
fd._write_song("x", "", False, 120, True, threading.Event())
check("clean" in fd._chat_llama.last["instructions"], "18+ off: lyrics kept clean")
fd._chat_llama = writer_answer(json.dumps({"tags": "rock", "key": "H major", "bpm": "fast"}))
w = fd._write_song("x", "", False, 60, False, threading.Event())
check(w and w["key"] is None and w["bpm"] is None, "bad key and bpm dropped")
fd._chat_llama = writer_answer("sorry, no")
check(fd._write_song("x", "", False, 60, False, threading.Event()) is None, "non-JSON answer -> None")
fd._enhance_port = lambda: None
check(fd._write_song("x", "", False, 60, False, threading.Event()) is None, "no chat model -> None")

# job_song with ComfyUI stubbed
ran = {}


async def fake_run(wf, emit, cancel, **kw):
    ran["wf"], ran["kw"] = wf, kw
    return b"ID3fake"

fd._comfy_run = fake_run
fd._song_ready = lambda: True
fd._worker_alive = lambda: False
fd._room_for_pictures = lambda: None
fd.COMFY.ensure_current = lambda status=None: None


def run(p):
    out = []
    fd.job_song(p, out.append, threading.Event())
    return out


fd._enhance_port = lambda: 1
fd._chat_llama = writer_answer(good)
out = run({"prompt": "a rainy day", "seconds": 999})
a = [o for o in out if o.get("t") == "audio"]
check(len(a) == 1 and base64.b64decode(a[0]["v"]) == b"ID3fake", "one audio frame with the file")
check(a[0]["seconds"] == 240 and ran["wf"]["5"]["inputs"]["seconds"] == 240.0, "seconds capped at 240")
enc = ran["wf"]["3"]["inputs"]
check(enc["tags"].startswith("indie pop") and enc["lyrics"].startswith("[Verse 1]") and enc["bpm"] == 180,
      "writer's tags, lyrics and bpm reach the graph")
check(ran["kw"].get("audio") is True, "_comfy_run asked for audio")

out = run({"prompt": "chill beat", "instrumental": True})
check(ran["wf"]["3"]["inputs"]["lyrics"] == "[Instrumental]", "instrumental -> no lyrics")
out = run({"prompt": "my song", "lyrics": "[Chorus]\nmine"})
check(ran["wf"]["3"]["inputs"]["lyrics"] == "[Chorus]\nmine", "the owner's lyrics are kept")

fd._enhance_port = lambda: None
out = run({"prompt": "no brain"})
check(any(o.get("t") == "note" for o in out) and ran["wf"]["3"]["inputs"]["lyrics"] == "[Instrumental]",
      "no chat model: instrumental, with a note")
check(ran["wf"]["3"]["inputs"]["tags"] == "no brain", "no chat model: the idea is the tags")

try:
    run({"prompt": " "})
    check(False, "empty idea refused")
except fd.UserFacing:
    check(True, "")
fd._song_ready = lambda: False
try:
    run({"prompt": "x"})
    check(False, "missing model refused")
except fd.UserFacing as e:
    check("isn't on your computer" in str(e), "missing model says so")

check(fd._job_kind("song", {}) == "song" and "song" in fd.QUEUED_OPS and "audio" in fd.RESULT_TYPES,
      "queued as a song; the audio frame is a held result")

print(f"{checks} checks, {fails} failed")
raise SystemExit(1 if fails else 0)
