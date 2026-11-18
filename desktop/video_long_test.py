"""Checks of longer videos (11-18): the part maths, and the chaining of parts with a faked ComfyUI. Fakes only: no GPU,
no ComfyUI, no network, nothing written under the real data folder.

    set FIXER_COMFY=0 && python desktop\\video_long_test.py        (the install's venv: needs Pillow, numpy, av)

FIXER_HOME is pointed at a throwaway folder (made and deleted here).
"""
from __future__ import annotations

import base64
import io
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="fixer_video_test_")
os.environ["FIXER_HOME"] = TMP
os.environ["FIXER_COMFY"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixer_desktop as fd  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(cond, msg: str):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


# ------------------------------------------------------------------ the part maths

def case_parts():
    for secs, frames, n in ((5, 81, 1), (10, 161, 2), (20, 321, 4), (30, 481, 6), (1, 17, 1), (2.5, 41, 1)):
        total, parts = fd._video_parts(secs, 30)
        check(total == frames and len(parts) == n, f"{secs} s -> {frames} frames in {n} parts, got {total} {parts}")
    for secs in (1, 2.5, 5, 5.2, 6, 7.3, 9.9, 10, 12, 15, 17.5, 20, 23, 29.9, 30):
        total, parts = fd._video_parts(secs, 30)
        check(all((x - 1) % 4 == 0 for x in parts), f"{secs} s: every part is 4k+1 frames {parts}")
        check(all(x <= fd.WAN14_PART for x in parts), f"{secs} s: no part over {fd.WAN14_PART} frames {parts}")
        check(1 + sum(x - 1 for x in parts) == total, f"{secs} s: joined parts make the whole clip {parts} {total}")
        check(abs((total - 1) / fd.WAN14_FPS - secs) <= 2 / fd.WAN14_FPS, f"{secs} s: length within two frames")
        check(max(parts) - min(parts) <= 4, f"{secs} s: parts as even as possible {parts}")
        check(len(parts) == 1 or min(parts) >= 41, f"{secs} s: no tiny last part {parts}")
    check(fd._video_parts(45, 30)[0] == 481, "over the limit is clamped to video_max_seconds")
    check(fd._video_parts(60, 60)[0] == 961 and len(fd._video_parts(60, 60)[1]) == 12, "a raised limit is honoured")
    check(fd._video_parts(0, 30)[0] == 17 and fd._video_parts(-3, 30)[0] == 17, "under 1 s is clamped to 1 s")
    for bad in (None, "abc", float("nan"), {}):
        check(fd._video_parts(bad, 30)[0] == 81, f"a bad value ({bad!r}) means the 5 s default")
    check(fd._video_parts("10", 30)[0] == 161, "a number sent as text still works")
    os.environ["FIXER_VIDEO_MAX_SECONDS"] = "10"
    try:
        check(fd._video_parts(30)[0] == 161, "the video_max_seconds knob is read live")
    finally:
        del os.environ["FIXER_VIDEO_MAX_SECONDS"]
    check(fd._video_parts(30)[0] == 481, "default limit 30 s")
    seeds = [fd._part_seed(1234, i) for i in range(6)]
    check(seeds[0] == 1234 and len(set(seeds)) == 6 and all(0 <= s < 2 ** 32 for s in seeds),
          "part 1 keeps the seed, later parts differ, all in range")
    check(seeds == [fd._part_seed(1234, i) for i in range(6)], "part seeds are reproducible")


# ------------------------------------------------------------------ chaining with a faked ComfyUI

def png(rgb, size=(16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="PNG")
    return buf.getvalue()


def ident(arr) -> tuple:
    a = np.asarray(arr)
    return tuple(int(x) for x in a[0, 0, :3])


class FakeWriter:
    last = None

    def __init__(self, fps):
        self.ids: list[tuple] = []
        self.n = 0
        self.discarded = False
        self.finished = False
        FakeWriter.last = self

    def add(self, frame):
        self.ids.append(ident(frame))
        self.n += 1

    def finish(self):
        self.finished = True
        return b"MP4" + bytes(self.n % 256 for _ in range(4))

    def discard(self):
        self.discarded = True


class Fake:
    """Stands in for ComfyUI: frame 0 of each part repeats its start picture (as Wan's does); the rest are new
    colours numbered 1, 2, 3... across the whole clip."""

    def __init__(self, cancel_in_part=None, fail_in_part=None):
        self.runs: list[dict] = []
        self.next_id = 1
        self.cancel_in_part = cancel_in_part
        self.fail_in_part = fail_in_part

    async def run(self, wf, emit, cancel, previews=False, frames=False, label=""):
        i = len(self.runs)
        name = wf["1"]["inputs"]["image"]
        path = fd.COMFY_PRIVATE / "input" / name
        start = ident(Image.open(path).convert("RGB")) if path.is_file() else None
        self.runs.append({"name": name, "existed": path.is_file(), "start": start,
                          "length": wf["12"]["inputs"]["length"], "seed": wf["13"]["inputs"]["noise_seed"],
                          "size": (wf["12"]["inputs"]["width"], wf["12"]["inputs"]["height"]),
                          "prompt": wf["10"]["inputs"]["text"],
                          "loras": sorted(n["inputs"]["lora_name"] for k, n in wf.items() if k.startswith(("80", "90"))),
                          "label": label})
        emit({"t": "status", "v": label})
        for node in ("13", "14"):
            for k in range(1, 3):
                emit({"t": "progress", "v": k / 2, "node": node})
        if self.fail_in_part == i:
            raise fd.UserFacing("The picture engine failed: fake")
        if self.cancel_in_part == i:
            cancel.set()
            return []
        out = [png(start)]
        for _ in range(wf["12"]["inputs"]["length"] - 1):
            k = self.next_id
            self.next_id += 1
            out.append(png((k % 256, k // 256, 7)))
        return out


def run_video(payload, fake, emitted=None):
    fd._comfy_run = fake.run
    frames = [] if emitted is None else emitted
    cancel = threading.Event()
    fd._TL.rec = fd._new_timing_record("video")
    try:
        fd._comfy_video(payload, frames.append, cancel)
        return frames, dict(fd._TL.rec)
    finally:
        fd._TL.rec = None


def photo_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), (200, 10, 10)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def leftovers() -> list[str]:
    folder = fd.COMFY_PRIVATE / "input"
    return [p.name for p in folder.iterdir()] if folder.is_dir() else []


def setup():
    fd._room_for_video = lambda status: None
    fd._worker_alive = lambda: False
    fd.COMFY.ensure_current = lambda status=None: None
    fd._explicit_video = lambda prompt, mode=None: 0.0
    fd._job_loras = lambda requested, family, extra_always_on=True: ([("hi.safetensors", "lo.safetensors", 0.7)], [])
    fd._Mp4Writer = FakeWriter


def case_chain_30s():
    setup()
    fake = Fake()
    frames, rec = run_video({"prompt": "waves roll in", "image": photo_b64(), "seconds": 30, "seed": 1234}, fake)
    runs = fake.runs
    check(len(runs) == 6 and all(r["length"] == 81 for r in runs), f"30 s = six 81-frame parts {[r['length'] for r in runs]}")
    check(all(r["existed"] for r in runs), "every part's start picture was on disk when its run started")
    check(runs[0]["start"] == (200, 10, 10), "part 1 starts from the photo")
    w = FakeWriter.last
    for i in range(1, len(runs)):
        check(runs[i]["start"] == w.ids[(81 + 80 * (i - 1)) - 1],
              f"part {i + 1} starts from part {i}'s last frame")
    check(len(w.ids) == 481 and len(set(w.ids)) == 481, f"481 frames joined, none repeated ({len(set(w.ids))} distinct)")
    check(len({r["name"] for r in runs}) == 6, "a fresh start picture name per part")
    seeds = [r["seed"] for r in runs]
    check(seeds[0] == 1234 and len(set(seeds)) == 6, "a different seed per part, part 1 keeps the job's")
    check(len({r["prompt"] for r in runs}) == 1 and len({tuple(r["loras"]) for r in runs}) == 1
          and len({r["size"] for r in runs}) == 1, "same words, add-ons and size for every part")
    check(runs[0]["loras"] == ["hi.safetensors", "lo.safetensors"], "the add-on pair is on every part")
    vids = [f for f in frames if f.get("t") == "video"]
    check(len(vids) == 1 and vids[0]["seconds"] == 30.0 and vids[0]["seed"] == 1234, f"one 30 s video {vids[:1] and vids[0].get('seconds')}")
    prog = [f["v"] for f in frames if f.get("t") == "progress"]
    check(all(b > a for a, b in zip(prog, prog[1:])), "the bar only moves forward")
    check(prog and prog[-1] == 1.0 and 1.0 not in prog[:-1] and prog[0] < 0.2, "the bar reaches 1.0 only at the end")
    texts = [f["v"] for f in frames if f.get("t") == "status"]
    check(any("part 2 of 6" in t for t in texts) and any("part 6 of 6" in t for t in texts), "status names the part")
    check(rec.get("count") == 481, f"the timing record counts every frame ({rec.get('count')})")
    check(leftovers() == [], f"no start picture left behind {leftovers()}")
    check(w.finished and not w.discarded, "the clip was finished")

    fake2 = Fake()
    run_video({"prompt": "waves roll in", "image": photo_b64(), "seconds": 30, "seed": 1234}, fake2)
    check([r["seed"] for r in fake2.runs] == seeds, "a fixed seed reproduces every part's seed")


def case_chain_5s_unchanged():
    setup()
    fake = Fake()
    frames, rec = run_video({"prompt": "x", "image": photo_b64(), "seed": 9}, fake)
    check(len(fake.runs) == 1 and fake.runs[0]["length"] == 81 and fake.runs[0]["seed"] == 9, "5 s: one run as before")
    check(fake.runs[0]["label"] == "Creating video…", "5 s: the plain label")
    w = FakeWriter.last
    check(len(w.ids) == 81 and w.ids[0] == (200, 10, 10), "5 s keeps all 81 frames, the first is the photo")
    vids = [f for f in frames if f.get("t") == "video"]
    check(vids and vids[0]["seconds"] == 5.0, "5 s reported")
    check(leftovers() == [], "nothing left behind (5 s)")


def case_odd_length():
    setup()
    fake = Fake()
    frames, _ = run_video({"prompt": "x", "image": photo_b64(), "seconds": 7.3, "seed": 3}, fake)
    lens = [r["length"] for r in fake.runs]
    w = FakeWriter.last
    check(lens == [61, 57] and len(w.ids) == 117 and len(set(w.ids)) == 117, f"7.3 s: two parts {lens}, 117 frames")
    vids = [f for f in frames if f.get("t") == "video"]
    check(vids and abs(vids[0]["seconds"] - 7.25) < 0.01, "7.3 s reports its real length (7.25 s)")


def case_cancel_midway():
    setup()
    fake = Fake(cancel_in_part=2)
    frames, _ = run_video({"prompt": "x", "image": photo_b64(), "seconds": 20, "seed": 5}, fake)
    check(len(fake.runs) == 3, "stops in part 3 of 4")
    check(not [f for f in frames if f.get("t") == "video"], "a cancelled clip returns nothing")
    check(FakeWriter.last.discarded and not FakeWriter.last.finished, "a cancelled clip is discarded")
    check(leftovers() == [], f"no start picture left after a cancel {leftovers()}")
    check(1.0 not in [f["v"] for f in frames if f.get("t") == "progress"], "the bar never says done on a cancel")


def case_cancel_between_parts():
    setup()
    fake = Fake()
    cancel = threading.Event()
    real = fake.run

    async def run(wf, emit, cancel_, **kw):
        out = await real(wf, emit, cancel_, **kw)
        if len(fake.runs) == 1:
            cancel_.set()                              # the stop arrives after part 1 finished, before part 2
        return out
    fd._comfy_run = run
    frames: list[dict] = []
    fd._comfy_video({"prompt": "x", "image": photo_b64(), "seconds": 10}, frames.append, cancel)
    check(len(fake.runs) == 1, "a stop between parts starts no further part")
    check(not [f for f in frames if f.get("t") == "video"] and leftovers() == [], "nothing returned, nothing left")


def case_failure_midway():
    setup()
    fake = Fake(fail_in_part=1)
    try:
        run_video({"prompt": "x", "image": photo_b64(), "seconds": 10}, fake)
        check(False, "a failed part must raise")
    except fd.UserFacing:
        pass
    check(FakeWriter.last.discarded, "a failed clip is discarded")
    check(leftovers() == [], f"no start picture left after a failure {leftovers()}")


def case_words_only():
    """From words: the first frame is drawn first (one more _comfy_run), then the parts as usual."""
    setup()
    fd._auto_models = lambda: {"picture": ["x"], "picture_explicit": ["x"]}
    fd._first_ready = lambda ids: {"defaults": {}, "builder": "fake"}
    fd._recipe_workflow = lambda r, v, w, h, seed, loras=None: {"draw": True}
    fake = Fake()
    real = fake.run

    async def run(wf, emit, cancel, **kw):
        if wf.get("draw"):
            return png((1, 2, 3), (64, 36))
        return await real(wf, emit, cancel, **kw)
    fd._comfy_run = run
    frames: list[dict] = []
    fd._comfy_video({"prompt": "a calm lake", "seconds": 10, "seed": 2}, frames.append, threading.Event())
    check(len(fake.runs) == 2 and fake.runs[0]["start"] == (1, 2, 3), "words: the drawn frame starts part 1")
    check([f for f in frames if f.get("t") == "video"] and leftovers() == [], "words: a video, nothing left behind")


def case_real_writer():
    """The real encoder, fed part by part, makes one playable clip with every frame."""
    import av
    fd._Mp4Writer = cls = REAL_WRITER
    wr = cls(16)
    for k in range(6):
        wr.add(np.full((48, 64, 3), k * 30, np.uint8))
    wr.add(np.full((48, 64, 3), 0.5, np.float32))     # float frames (the old diffusers path) still work
    for k in range(5):
        wr.add(np.full((48, 64, 3), 200 - k * 20, np.uint8))
    data = wr.finish()
    with av.open(io.BytesIO(data)) as c:
        n = sum(1 for _ in c.decode(video=0))
    check(len(data) > 0 and n == 12, f"12 frames in, 12 frames out ({n})")
    check(len(fd._encode_mp4(np.zeros((5, 32, 32, 3), np.float32), 16)) > 0, "_encode_mp4 still takes a whole array")
    bad = cls(16)
    bad.add(np.zeros((32, 32, 3), np.uint8))
    bad.discard()
    check(bad.finish() == b"", "a discarded writer gives nothing back")


REAL_WRITER = fd._Mp4Writer


def main():
    for name, case in list(globals().items()):
        if name.startswith("case_"):
            case()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{CHECKS} checks, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
