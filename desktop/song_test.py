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
good = json.dumps({"title": "Rain Town", "tags": "indie pop, female vocal, warm", "lyrics": "[Verse 1]\nla la la in the rain",
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
w = fd._write_song("x", "", True, 60, False, threading.Event())
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
check(a[0]["seconds"] == 360 and ran["wf"]["5"]["inputs"]["seconds"] == 360.0, "seconds capped at 360")
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

# auto length: the writer's seconds, else from the lyrics
fd._song_ready = lambda: True
fd._enhance_port = lambda: 1
fd._chat_llama = writer_answer(json.dumps({"title": "T", "tags": "rock", "lyrics": "[Verse]\n" + "line\n" * 20,
                                           "bpm": 100, "key": "E minor", "seconds": 200}))
out = run({"prompt": "rock song", "seconds": "auto"})
check(ran["wf"]["5"]["inputs"]["seconds"] == 200.0, "auto: the writer's length")
check('"seconds"' in fd._chat_llama.last["instructions"], "auto: the writer is asked for a length")
fd._chat_llama = writer_answer(json.dumps({"title": "T", "tags": "rock", "lyrics": "[Verse]\n" + "line\n" * 20}))
out = run({"prompt": "rock song"})
check(ran["wf"]["5"]["inputs"]["seconds"] == float(25 + 4 * 20), "auto without the writer's length: from the lyrics")

# chosen sound, language
fd._chat_llama = writer_answer(good)
out = run({"prompt": "x", "tags": "synthwave, male vocal", "language": "es", "seconds": 60})
ins = fd._chat_llama.last["instructions"]
check("synthwave, male vocal" in ins and "Spanish" in ins, "the chosen sound and language reach the writer")
check(ran["wf"]["3"]["inputs"]["language"] == "es", "the language reaches the singer")
out = run({"prompt": "x", "language": "xx", "seconds": 60})
check(ran["wf"]["3"]["inputs"]["language"] == "en", "an unknown language falls back to English")

# another take: no writing, same words
calls = []
fd._chat_llama = lambda *a, **k: calls.append(1)
out = run({"prompt": "x", "tags": "jazz, female vocal", "lyrics": "[Verse]\nmine", "write": False, "seconds": 90,
           "title": "Old", "bpm": 90, "key": "D minor", "seed": 7})
enc = ran["wf"]["3"]["inputs"]
check(not calls and enc["tags"] == "jazz, female vocal" and enc["lyrics"] == "[Verse]\nmine" and enc["bpm"] == 90
      and enc["keyscale"] == "D minor", "another take sings the given song as it is")
a = [o for o in out if o.get("t") == "audio"][0]
check(a["title"] == "Old" and a["seed"] == 7, "another take keeps the title")

# write first
fd._chat_llama = writer_answer(good)
out = []
fd.job_song_write({"prompt": "rain", "seconds": 0}, out.append, threading.Event())
plan = [o for o in out if o.get("t") == "song_plan"]
check(len(plan) == 1 and plan[0]["lyrics"].startswith("[Verse 1]") and "song_plan" not in fd.QUEUED_OPS
      and "song_write" in fd.IMMEDIATE, "write first: a plan, no queue, no card")

# album
plan_json = json.dumps({"title": "Coast", "sound": "surf rock, reverb guitar", "cover": "a beach at dusk",
                        "tracks": [{"title": "One", "idea": "a"}, {"title": "Two", "idea": "b"},
                                   {"title": "Three", "idea": "c"}]})
answers = [plan_json, good, good, good]


def seq_chat(p, emit, cancel, port):
    seq_chat.calls.append(p)
    emit({"t": "text", "v": answers[len(seq_chat.calls) - 1]})
seq_chat.calls = []
fd._chat_llama = seq_chat
fd._album_cover = lambda prompt, seed, cancel: b"PNG"
out = []
fd.job_album({"prompt": "summer at the coast", "tracks": 3}, out.append, threading.Event())
kinds = [o["t"] for o in out if o["t"] in ("album", "cover", "audio")]
check(kinds == ["album", "cover", "audio", "audio", "audio"], "album: plan, cover, then each track")
aud = [o for o in out if o["t"] == "audio"]
check([x["track"] for x in aud] == [1, 2, 3] and len({x["album_id"] for x in aud}) == 1
      and all(x["album_title"] == "Coast" for x in aud), "album tracks carry the album and their number")
check("surf rock" in seq_chat.calls[1]["instructions"], "each track is written in the album's sound")
check(len({x["seed"] for x in aud}) == 3, "each track has its own seed")
prog = [o["v"] for o in out if o["t"] == "progress"]
check(prog == sorted(prog), "album progress only goes up")
check(fd._job_kind("album", {}) == "album" and "album" in fd.QUEUED_OPS and "cover" in fd.RESULT_TYPES,
      "albums are queued; covers are held results")

# album stopped after the first track keeps it
seq_chat.calls = []
ev = threading.Event()


async def stop_after_one(wf, emit, cancel, **kw):
    if stop_after_one.n:
        ev.set()
        return b""
    stop_after_one.n += 1
    return b"ID3"
stop_after_one.n = 0
fd._comfy_run = stop_after_one
out = []
fd.job_album({"prompt": "x", "tracks": 3}, out.append, ev)
check(len([o for o in out if o["t"] == "audio"]) == 1, "a stopped album keeps the finished track")
fd._comfy_run = fake_run

# line breaks inside the JSON lyrics (what the model really writes) still parse; no voice named -> "vocals"
fd._song_ready = lambda: True
fd._enhance_port = lambda: 1
NL = chr(10)                                           # a real line break inside the JSON string
fd._chat_llama = writer_answer('{"title": "N", "tags": "folk, acoustic", "lyrics": "[Verse 1]' + NL
                               + 'line one is here' + NL + 'line two is here", "bpm": 90, "key": "G major"}')
w = fd._write_song("x", "", False, 60, False, threading.Event())
check(w is not None and "line two" in w["lyrics"], "raw line breaks inside the lyrics parse")
check(w["tags"].endswith(", vocals"), "no voice named: vocals added")
w = fd._write_song("x", "", True, 60, False, threading.Event())
check(not w["tags"].endswith(", vocals"), "instrumental: no vocals added")
tries = []


def empty_lyrics(p, emit, cancel, port):
    tries.append(1)
    emit({"t": "text", "v": json.dumps({"tags": "pop, female vocal", "lyrics": ""}) if len(tries) == 1 else good})
fd._chat_llama = empty_lyrics
w = fd._write_song("x", "", False, 60, False, threading.Event())
check(len(tries) == 2 and w["lyrics"].startswith("[Verse 1]"), "no lyrics on the first try: asked once more")

# artists as inspiration: described in words, never named
check(fd._artists_text([{"name": " Some  Band ", "genres": ["indie rock", "post-punk"]}, "Solo Singer", 5, {}])
      == "Some Band (indie rock, post-punk); Solo Singer", "artist picks become plain text")
check(len(fd._artists_text([{"name": f"A{i}"} for i in range(9)]).split("; ")) == 5, "at most 5 artists")
fd._chat_llama = writer_answer(good)
fd._write_song("x", "", False, 60, False, threading.Event(), artists="Some Band (indie rock)")
ins = fd._chat_llama.last["instructions"]
check("Inspired by: Some Band (indie rock)" in ins and "Never put an artist's name" in ins, "the writer gets the artists")
out = run({"prompt": "x", "artists": [{"name": "Some Band"}], "seconds": 60})
check("Some Band" in fd._chat_llama.last["instructions"], "a song's artists reach the writer")
check("Inspired by" not in fd._song_system(60, False, False, False), "no artists: nothing added")

# a reference recording: cover mode, written for the job and deleted after
seen_files = []


async def ref_run(wf, emit, cancel, **kw):
    ran["wf"] = wf
    name = wf.get("9", {}).get("inputs", {}).get("audio")
    if name:
        seen_files.append((fd.COMFY_PRIVATE / "input" / name).is_file())
    return b"ID3"
fd._comfy_run = ref_run
fd._chat_llama = writer_answer(good)
out = run({"prompt": "x", "reference": base64.b64encode(b"RIFFfake").decode(), "reference_ext": "wav",
           "reference_seconds": 95})
wf = ran["wf"]
name = wf["9"]["inputs"]["audio"]
check(seen_files == [True] and not (fd.COMFY_PRIVATE / "input" / name).exists(), "the recording exists only for the job")
check(name.endswith(".wav") and wf["6"]["inputs"]["latent_image"] == ["11", 0]
      and wf["6"]["inputs"]["positive"] == ["3", 0] and "12" not in wf
      and wf["3"]["inputs"]["generate_audio_codes"] is True and wf["6"]["inputs"]["denoise"] == 0.55,
      "the recording is the starting point; lyrics and style still plan the song (codes on)")
check(wf["5"]["inputs"]["seconds"] == 95.0 and wf["10"]["inputs"]["duration"] == 95.0, "auto length = the recording's")
out = run({"prompt": "x", "reference": base64.b64encode(b"x").decode(), "reference_seconds": 40, "seconds": 120,
           "reference_keep": "close"})
check(ran["wf"]["10"]["inputs"]["duration"] == 40.0 and ran["wf"]["6"]["inputs"]["denoise"] == 0.4,
      "no longer than the recording; Close keeps more of it")
out = run({"prompt": "x", "reference": base64.b64encode(b"x").decode(), "reference_ext": "exe", "seconds": 60})
check(ran["wf"]["9"]["inputs"]["audio"].endswith(".m4a"), "an odd extension becomes m4a")
try:
    run({"prompt": "x", "reference": base64.b64encode(b"x" * (fd.SONG_REF_MAX + 1)).decode()})
    check(False, "too large refused")
except fd.UserFacing:
    check(True, "")
out = run({"prompt": "x", "seconds": 60})
check("9" not in ran["wf"], "no reference: the plain graph")

# a link: fetched by yt-dlp for the job, its length used for Auto, deleted after
import sys
import types


class FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download):
        FakeYDL.opts_seen = self.opts
        if "fail" in url:
            raise RuntimeError("nope")
        out = self.opts["outtmpl"].replace("%(ext)s", "m4a")
        open(out, "wb").write(b"m4a")
        return {"duration": 132}
sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=FakeYDL)
seen_files.clear()
out = run({"prompt": "x", "reference_url": "https://soundcloud.com/me/old-song"})
wf = ran["wf"]
check(seen_files == [True] and wf["9"]["inputs"]["audio"].endswith(".m4a")
      and not (fd.COMFY_PRIVATE / "input" / wf["9"]["inputs"]["audio"]).exists(), "a link: fetched for the job, then gone")
check(wf["5"]["inputs"]["seconds"] == 132.0, "a link: Auto length is the recording's")
check(FakeYDL.opts_seen["noplaylist"] and FakeYDL.opts_seen["max_filesize"] == fd.SONG_REF_MAX, "one file, capped")
check(any(o.get("v") == "Getting the recording from the link…" for o in out), "says it is getting the link")
for bad in ("ftp://x.com/a", "http://localhost/a", "http://192.168.1.5/a", "http://10.0.0.130:8088/x"):
    try:
        run({"prompt": "x", "reference_url": bad})
        check(False, "refused " + bad)
    except fd.UserFacing:
        check(True, "")
before_refs = set((fd.COMFY_PRIVATE / "input").glob("songref_*"))   # a live job may hold one of its own
try:
    run({"prompt": "x", "reference_url": "https://youtube.com/watch?v=fail"})
    check(False, "a failing link says so")
except fd.UserFacing as e:
    check("Couldn't get the audio" in str(e) and set((fd.COMFY_PRIVATE / "input").glob("songref_*")) == before_refs,
          "a failing link says so and leaves nothing")
fd._comfy_run = fake_run

# artwork: painted after the song when asked; or later on its own, from the song or the owner's wish
fd._comfy_run = fake_run
covers = []
fd._album_cover = lambda prompt, seed, cancel: covers.append(prompt) or b"PNG"
fd._chat_llama = writer_answer(good)
out = run({"prompt": "x", "seconds": 60, "artwork": True})
kinds = [o["t"] for o in out if o["t"] in ("audio", "cover")]
aud = [o for o in out if o["t"] == "audio"][0]
cov = [o for o in out if o["t"] == "cover"]
check(kinds == ["audio", "cover"] and aud["art_id"] == cov[0]["art_id"], "song artwork comes after the song, same key")
out = run({"prompt": "x", "seconds": 60})
check(not [o for o in out if o["t"] == "cover"] and "art_id" not in [o for o in out if o["t"] == "audio"][0],
      "no artwork unless asked")
fd._chat_llama = writer_answer(json.dumps({"cover": "a red guitar in the rain"}))
out = []
fd.job_artwork({"art_id": "abc", "title": "T", "tags": "rock"}, out.append, threading.Event())
c = [o for o in out if o["t"] == "cover"]
check(len(c) == 1 and c[0]["art_id"] == "abc" and covers[-1] == "a red guitar in the rain", "artwork later: designed")
out = []
fd.job_artwork({"art_id": "abc", "prompt": "my dog on a stage"}, out.append, threading.Event())
check("my dog on a stage" in fd._chat_llama.last["text"] if hasattr(fd._chat_llama, "last") and "text" in fd._chat_llama.last
      else "my dog on a stage" in json.dumps(fd._chat_llama.last), "the owner's wish reaches the designer")
fd._album_cover = lambda prompt, seed, cancel: None
try:
    fd.job_artwork({"art_id": "abc"}, [].append, threading.Event())
    check(False, "no picture model says so")
except fd.UserFacing:
    check(True, "")
check(fd._job_kind("artwork", {}) == "artwork" and "artwork" in fd.QUEUED_OPS, "artwork is queued")

# explicit lyrics switch: off clean; on + 18+ off strong language only; on + 18+ on explicit
fd._comfy_run = fake_run
fd._song_ready = lambda: True
fd._enhance_port = lambda: 1
fd._chat_llama = writer_answer(good)
for payload, want in (({}, "Keep the lyrics clean"), ({"safe": False}, "Keep the lyrics clean"),
                      ({"explicit_lyrics": True, "safe": True}, "nothing sexual."),
                      ({"explicit_lyrics": True, "safe": False}, "adult, sexual themes")):
    run({"prompt": "x", "seconds": 60, **payload})
    check(want in fd._chat_llama.last["instructions"], f"lyric rule for {payload}")

print(f"{checks} checks, {fails} failed")
raise SystemExit(1 if fails else 0)
