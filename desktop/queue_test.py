"""Pure-Python checks of the line, the notes and the brain's command line (11-17). Fakes only: no GPU, no ComfyUI,
no llama-server, no network, nothing written under the real data folder.

    set FIXER_COMFY=0 && python desktop\\queue_test.py        (any Python with aiohttp + cryptography)

FIXER_HOME is pointed at a throwaway folder (made and deleted here) so no keys or models are read or written.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="fixer_queue_test_")
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


# ------------------------------------------------------------------ fakes

ORDER: list[str] = []


def fake_target(op, payload):
    def run(p, emit, cancel):
        ORDER.append(p.get("name", "?"))
        if p.get("fail"):
            raise fd.UserFacing("boom")
        steps = max(1, int(p.get("secs", 0.1) / 0.02))
        for i in range(steps):
            if cancel.is_set():
                return
            emit({"t": "progress", "v": round((i + 1) / steps, 3)})
            time.sleep(0.02)
        for k in range(int(p.get("results", 1))):
            emit({"t": "image", "v": f"img{k}"})
    return run


fd._target = fake_target
fd._comfy_stop_now = lambda: None
fd.COMFY.free = lambda: None
FIGS = {"card": {"name": "card", "used_gb": 10.8, "total_gb": 15.9}, "memory": {"used_gb": 41.2, "total_gb": 63.9}}
fd._figures = lambda: FIGS


async def start():
    fd.QUEUE = fd.JobQueue()
    fd.GPU = asyncio.Lock()
    ORDER.clear()
    agen = fd.queue_runner(None)
    await agen.__anext__()
    return agen


async def stop(agen):
    try:
        await agen.__anext__()
    except StopAsyncIteration:
        pass
    await asyncio.sleep(0.05)


def mk(name, slot=None, phone="p1", secs=0.1, ack=False, op="image", **kw):
    p = {"name": name, "secs": secs, "prompt": name, **kw}
    if slot:
        p["slot"] = slot
    if ack:
        p["ack"] = True
    return fd.Job(op, p, phone)


def sub(job):
    return fd.QUEUE.submit(job, asyncio.get_running_loop())


async def watch(job, phone="p1", fail_after=None):
    frames: list[dict] = []

    async def send(obj):
        if fail_after is not None and len(frames) >= fail_after:
            raise ConnectionResetError()
        frames.append(obj)

    await fd._watch(job, send, phone)
    return frames


async def op_queue(payload, phone="p1"):
    frames: list[dict] = []

    async def send(obj):
        frames.append(obj)

    await fd._op_queue(payload, send, phone)
    return frames


def types_of(frames):
    return [f.get("t") for f in frames]


async def until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


# ------------------------------------------------------------------ cases

async def case_fifo():
    agen = await start()
    a, b, c = mk("A", "s1"), mk("B", "s2"), mk("C", "s3")
    for j in (a, b, c):
        sub(j)
    ws = [asyncio.create_task(watch(j)) for j in (a, b, c)]
    fa, fb, fc = await asyncio.gather(*ws)
    check(ORDER == ["A", "B", "C"], f"fifo order {ORDER}")
    check(types_of(fa)[0] == "queued" and types_of(fa)[-1] == "done", "A frames start queued end done")
    stat_c = [f["v"] for f in fc if f.get("t") == "status" and f["v"].startswith("In line")]
    check(stat_c and stat_c[-1] == "In line: 1st", f"C ends at 1st in line: {stat_c}")
    pos_c = [f["position"] for f in fc if f.get("t") == "queued"]
    check(pos_c == sorted(pos_c, reverse=True), f"C positions only fall: {pos_c}")
    check(all(f.get("n") == 1 for f in fb if f.get("t") == "image"), "result frames carry n")
    await stop(agen)


async def case_same_slot_waiting():
    agen = await start()
    r, a, b = mk("R", "r", secs=0.3), mk("A", "s"), mk("B", "t")
    for j in (r, a, b):
        sub(j)
    await until(lambda: r.state == "running")
    wa = asyncio.create_task(watch(a))
    a2 = mk("A2", "s")
    replaced = sub(a2)
    check(replaced == [a], "A replaced by A2")
    wb, wa2, wr = (asyncio.create_task(watch(j)) for j in (b, a2, r))
    fa = await wa
    await asyncio.gather(wb, wa2, wr)
    check(any(f.get("stopped") == "replaced" for f in fa if f.get("t") == "info"), "A told: replaced")
    check(fa[-1]["t"] == "error" and "replaced" in fa[-1]["v"], "A ends with the replaced text")
    check(ORDER == ["R", "A2", "B"], f"A2 took A's place: {ORDER}")
    await stop(agen)


async def case_same_slot_running():
    agen = await start()
    r, w = mk("R", "r", secs=2.0), mk("W", "w")
    sub(r)
    sub(w)
    wr = asyncio.create_task(watch(r))
    await until(lambda: r.state == "running")
    r2 = mk("R2", "r")
    replaced = sub(r2)
    check(replaced == [r], "running R replaced")
    wr2, ww = asyncio.create_task(watch(r2)), asyncio.create_task(watch(w))
    fr = await wr
    await asyncio.gather(wr2, ww)
    check(any(f.get("stopped") == "replaced" for f in fr if f.get("t") == "info"), "R told: replaced")
    check(ORDER == ["R", "R2", "W"], f"R2 runs before W: {ORDER}")
    check(not any(f.get("t") == "image" for f in fr), "stopped job sends no result")
    await stop(agen)


async def case_legacy():
    agen = await start()
    r = mk("R", "r", secs=0.3)
    sub(r)
    await until(lambda: r.state == "running")
    l1, l2, l3 = mk("L1", phone="p1"), mk("L2", phone="p1"), mk("L3", phone="p2")
    check(l1.slot == "legacy:p1", "legacy slot per phone")
    sub(l1)
    check(sub(l2) == [l1], "newest wins for the same phone")
    check(sub(l3) == [], "another phone isn't replaced")
    ws = [asyncio.create_task(watch(j, ph)) for j, ph in ((r, "p1"), (l2, "p1"), (l3, "p2"))]
    await asyncio.gather(*ws)
    check(ORDER == ["R", "L2", "L3"], f"legacy order {ORDER}")
    await stop(agen)


async def case_move():
    agen = await start()
    r = mk("R", "r", secs=0.4)
    a, b, c = mk("A", "a"), mk("B", "b"), mk("C", "c")
    for j in (r, a, b, c):
        sub(j)
    await until(lambda: r.state == "running")
    check(fd.QUEUE.position(c) == 3, "C is third")
    fr = await op_queue({"action": "move", "id": c.id, "to": 1})
    check(fr[0].get("moved") is True and fr[0]["position"] == 1, f"C moved to next: {fr}")
    check(fd.QUEUE.move(a, 99) == 3, "move clamps to the last place")
    fr = await op_queue({"action": "move", "id": r.id, "to": 1})
    check(fr[0]["t"] == "error", "a running job can't be moved")
    ws = [asyncio.create_task(watch(j)) for j in (r, a, b, c)]
    await asyncio.gather(*ws)
    check(ORDER == ["R", "C", "B", "A"], f"after moves {ORDER}")
    await stop(agen)


async def case_cancel_and_stop_all():
    agen = await start()
    r = mk("R", "r", secs=1.0)
    a, b, c = mk("A", "a"), mk("B", "b"), mk("C", "c")
    for j in (r, a, b, c):
        sub(j)
    await until(lambda: r.state == "running")
    wa = asyncio.create_task(watch(a))
    fr = await op_queue({"action": "cancel", "ids": [a.id]})
    check(fr[0] == {"t": "info", "cancelled": 1}, f"cancel by id: {fr}")
    check(any(f.get("stopped") == "cancelled" for f in await wa if f.get("t") == "info"), "A told: cancelled")
    fr = await op_queue({"action": "cancel", "slot": "b"}, "p1")
    check(fr[0]["cancelled"] == 1, "cancel by slot")
    fr = await op_queue({"action": "cancel", "slot": "c"}, "p2")
    check(fr[0]["cancelled"] == 0, "another phone's slot name cancels nothing")
    hidden = fd.Job("warm", {"name": "W", "for": "make"}, "p1")
    sub(hidden)
    wr = asyncio.create_task(watch(r))
    n = fd.QUEUE.stop_all("stop_all", True)
    check(n == 2, f"stop_all counts only real jobs (R and C): {n}")
    fr = await wr
    check(any(f.get("stopped") == "stop_all" for f in fr if f.get("t") == "info"), "R told: stop_all")
    await until(lambda: not fd.QUEUE.jobs)
    check(not fd.QUEUE.jobs, "line empty after stop_all")
    check(not fd.QUEUE.has_work(), "no work left")
    # a warm-up is dropped when a real job arrives
    w = fd.Job("warm", {"name": "W2", "for": "make"}, "p1")
    sub(w)
    real = mk("X", "x")
    sub(real)
    check(w.stop_reason == "replaced", "warm-up replaced by a real job")
    await watch(real)
    await stop(agen)


async def case_detach_and_collect():
    agen = await start()
    j = mk("D", "s", secs=0.3, ack=True)
    sub(j)
    first = await watch(j, fail_after=2)             # the phone goes away after two frames
    check(len(first) == 2, "watcher cut off")
    check(await until(lambda: j.state == "ready"), "job finished without its phone")
    check(fd.QUEUE.find(j.id) is j, "ready job is held")
    snap = fd.QUEUE.snapshot("p1")
    check(snap and snap[0]["state"] == "ready" and snap[0]["keep_s"] > 0, f"held job listed ready: {snap}")
    fr = await op_queue({"action": "watch", "id": j.id})
    check(fr[0]["t"] == "queued" and fr[0]["state"] == "ready" and fr[0]["words"] == "D", f"re-attach first frame {fr[0]}")
    imgs = [f for f in fr if f.get("t") == "image"]
    check(len(imgs) == 1 and imgs[0]["n"] == 1 and fr[-1]["t"] == "done", "re-attach gets the result (n=1) and done")
    check(fd.QUEUE.find(j.id) is j, "still held until acknowledged")
    fr = await op_queue({"action": "ack", "id": j.id})
    check(fr[0] == {"t": "info", "acked": True}, "ack answered")
    check(fd.QUEUE.find(j.id) is None, "acknowledged result is dropped")
    fr = await op_queue({"action": "watch", "id": j.id})
    check(fr[0].get("gone") and fr[1]["t"] == "error", f"gone after ack: {fr}")
    await stop(agen)


async def case_old_phone_keep_drop_expire():
    agen = await start()
    seen = mk("S", "s1", secs=0.1)                   # old phone, watcher attached at the end
    sub(seen)
    await watch(seen)
    check(fd.QUEUE.find(seen.id) is None, "delivered result of an old phone is dropped at once")
    missed = mk("M", "s2", secs=0.2)
    sub(missed)
    await watch(missed, fail_after=1)
    check(await until(lambda: missed.state == "ready"), "missed job finished")
    check(fd.QUEUE.find(missed.id) is missed, "undelivered result is held")
    missed.finished -= (fd._tune("keep_minutes", 30) + 1) * 60
    fd.QUEUE.expire()
    check(fd.QUEUE.find(missed.id) is None, "held result dropped after 30 minutes")
    check("30 minutes" in fd.QUEUE.gone[missed.id][0], "expired job explains itself")
    await stop(agen)


async def case_failure():
    agen = await start()
    f1 = mk("F1", "s1", fail=True)
    sub(f1)
    fr = await watch(f1)
    check(fr[-1] == {"t": "error", "v": "boom"}, f"failure delivered: {fr[-1]}")
    check(fd.QUEUE.find(f1.id) is None, "delivered failure is dropped")
    f2 = mk("F2", "s2", fail=True)
    sub(f2)
    await watch(f2, fail_after=1)
    check(await until(lambda: f2.state == "failed"), "failed without a watcher")
    check(fd.QUEUE.find(f2.id) is f2, "undelivered failure is held")
    snap = fd.QUEUE.snapshot()
    check(snap[0]["state"] == "failed" and snap[0]["error"] == "boom", "held failure shows its error")
    fr = await op_queue({"action": "watch", "id": f2.id})
    check(fr[-1] == {"t": "error", "v": "boom"}, "re-attach to a failed job gets the error")
    await stop(agen)


async def case_full_line():
    agen = await start()
    os.environ["FIXER_QUEUE_MAX"] = "2"
    try:
        r = mk("R", "r", secs=0.3)
        sub(r)
        await until(lambda: r.state == "running")
        sub(mk("A", "a"))
        sub(mk("B", "b"))
        try:
            sub(mk("C", "c"))
            check(False, "a full line must refuse")
        except fd.UserFacing as e:
            check("waiting" in str(e), f"full-line text: {e}")
    finally:
        del os.environ["FIXER_QUEUE_MAX"]
    fd.QUEUE.stop_all()
    await until(lambda: not fd.QUEUE.jobs)
    await stop(agen)


def case_notes():
    check([fd._ordinal(k) for k in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 111, 112, 113)] ==
          ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "111th", "112th", "113th"],
          "ordinals")
    check(fd._queue_note([], "chat:x", FIGS, False) == "", "no note for an empty line")
    jobs = [
        {"id": "a1b2c3", "kind": "video", "label": "a cat surfing at sunset", "state": "running", "progress": 0.42,
         "eta_s": 110, "slot": "chat:x"},
        {"id": "d4e5f6", "kind": "picture", "label": "red sports car", "state": "waiting", "position": 1,
         "slot": "make:create"},
        {"id": "9f8e7d", "kind": "picture", "label": "old fisherman", "state": "ready", "slot": "chat:y"},
        {"id": "0c0c0c", "kind": "edit", "label": "make it night", "state": "failed", "slot": "make:change"},
    ]
    want = "\n".join([
        "(Live status from my computer, added automatically, not part of my message:",
        '- job a1b2c3: video "a cat surfing at sunset", being made now, 42% done, about 2 min left, from this chat',
        '- job d4e5f6: picture "red sports car", waiting, 1st in line, from Make',
        '- job 9f8e7d: picture "old fisherman", finished, waiting for my phone to collect it, from another chat',
        '- job 0c0c0c: photo edit "make it night", didn\'t work',
        "Graphics card memory: 10.8 of 15.9 GB in use. Computer memory: 41.2 of 63.9 GB in use. "
        "The chat is paused: no.)"])
    got = fd._queue_note(jobs, "chat:x", FIGS, False)
    check(got == want, f"note text:\n{got}")
    check("The chat is paused: yes." in fd._queue_note(jobs, "chat:x", FIGS, True), "note says the chat is paused")

    p = {"instructions": "Be nice.", "slot": "chat:x", "_queue": jobs,
         "messages": [{"role": "user", "text": "first"}, {"role": "assistant", "text": "ok"},
                      {"role": "user", "text": "stop the video"}]}
    out = fd._with_queue(p)
    check(out["instructions"].startswith("Be nice.") and "queue_cancel" in out["instructions"], "tools appended")
    check(out["messages"][0]["text"] == "first" and out["messages"][1]["text"] == "ok", "earlier turns untouched")
    check(out["messages"][2]["text"].startswith("stop the video\n\n(Live status"), "note on the last user message")
    check(p["messages"][2]["text"] == "stop the video", "input not mutated")
    empty = fd._with_queue({"instructions": "", "slot": "chat:x", "_queue": [], "messages": [{"role": "user", "text": "hi"}]})
    check(empty["messages"][0]["text"] == "hi", "no note when the line is empty")
    fd.PHONE_CONFIG.write_text('{"queue_tools": "CUSTOM TOOLS"}', encoding="utf-8")
    check(fd._with_queue({"instructions": "x", "_queue": [], "messages": []})["instructions"].endswith("CUSTOM TOOLS"),
          "queue_tools read from phone_config.json")
    fd.PHONE_CONFIG.unlink()


def case_kinds():
    check(fd._job_kind("video", {}) == "video" and fd._job_kind("cutout", {}) == "cutout", "plain kinds")
    check(fd._job_kind("image", {"image": "x"}) == "edit" and fd._job_kind("image", {}) == "picture", "image kinds")
    check(fd._job_kind("comfy_run", {"values": {"image_mode": "paint", "image": "x"}}) == "change", "paint = change")
    check(fd._job_kind("comfy_run", {"values": {"image": "x"}}) == "edit", "comfy edit")
    check(fd._job_kind("comfy_run", {"values": {"count": 3}}) == "pictures", "several pictures")
    check(fd._job_kind("comfy_run", {"values": {}}) == "picture", "one picture")
    long = fd._job_label("image", {"prompt": "word " * 40})
    check(len(long) == 61 and long.endswith("…"), "label cut at 60 + ellipsis")
    check(fd._job_label("image", {"label": "Remove the dog", "prompt": "zzz"}) == "Remove the dog", "phone label wins")


def case_brain():
    sys.modules.pop("huggingface_hub", None)

    class Info:
        def __init__(self, files):
            self.siblings = [types.SimpleNamespace(rfilename=n, size=int(z * 1e9)) for n, z in files]

    repos = {
        fd.MOE_BRAIN: [("M-Q2_K.gguf", 12), ("M-Q4_K_S.gguf", 19.0), ("M-Q8_0.gguf", 35),
                       ("M-mmproj-F16.gguf", 0.9), ("M-mmproj-BF16.gguf", 0.8)],
        fd.DENSE_BRAIN: [("D-Q3_K_L.gguf", 14.3), ("D-Q4_K_M.gguf", 16.6), ("D-Q5_K_M.gguf", 19.0),
                         ("D-mmproj-Q8_0.gguf", 0.9)],
        "someone/Foo-30B-A3B-GGUF": [("F-Q4_K_M.gguf", 18.0), ("F-Q2_K.gguf", 11)],
    }

    class Api:
        def model_info(self, repo, files_metadata=False):
            return Info(repos[repo])

    sys.modules["huggingface_hub"] = types.SimpleNamespace(HfApi=Api)
    fd._GGUF_PICK.clear()
    try:
        files, weights, proj = fd.gguf_choice(fd.MOE_BRAIN)
        check(weights == "M-Q4_K_S.gguf" and proj == "M-mmproj-BF16.gguf", f"MoE pin: {weights} {proj}")
        check(len(files) == 2, "MoE downloads just those two files")
        _, weights, proj = fd.gguf_choice(fd.DENSE_BRAIN)
        check(weights == "D-Q3_K_L.gguf" and proj == "D-mmproj-Q8_0.gguf", f"dense still fits the card: {weights}")
        _, weights, _ = fd.gguf_choice("someone/Foo-30B-A3B-GGUF")
        check(weights == "F-Q4_K_M.gguf", f"unpinned MoE name uses the roomy budget: {weights}")
    finally:
        sys.modules.pop("huggingface_hub", None)
        fd._GGUF_PICK.clear()

    check(fd._is_moe(fd.MOE_BRAIN) and fd._is_moe("x/Foo-30B-A3B-GGUF") and not fd._is_moe(fd.DENSE_BRAIN)
          and not fd._is_moe(None), "_is_moe")
    check(fd.DEFAULT_BRAIN == fd.MOE_BRAIN and fd.BRAIN_ALIASES["huihui-ai/Huihui-Qwen3-VL-8B-Instruct-abliterated"]
          == fd.MOE_BRAIN, "MoE is the default and old ids resolve to it")

    folder = Path(TMP) / "w"
    folder.mkdir(exist_ok=True)
    (folder / "w.gguf").write_bytes(b"x" * 1000)
    a = fd._llama_args(fd.MOE_BRAIN, folder, "w.gguf", "p.gguf", True, 1234)
    check("--cpu-moe" in a and "--no-mmproj-offload" not in a and a[a.index("--fit") + 1] == "on", f"MoE args {a}")
    check("--spec-type" not in a and "-ngl" not in a and a[a.index("-t") + 1] == "7", "MoE defaults")
    os.environ["FIXER_BRAIN_NCMOE"] = "10"
    os.environ["FIXER_BRAIN_MTP"] = "1"
    try:
        b = fd._llama_args(fd.MOE_BRAIN, folder, "w.gguf", None, True, 1234)
        check("--cpu-moe" not in b and b[b.index("--n-cpu-moe") + 1] == "10", "n-cpu-moe knob")
        check("draft-mtp" in b and "--mmproj" not in b, "mtp knob, no projector")
    finally:
        del os.environ["FIXER_BRAIN_NCMOE"], os.environ["FIXER_BRAIN_MTP"]
    d = fd._llama_args(fd.DENSE_BRAIN, folder, "w.gguf", "p.gguf", True, 1234)
    check("--cpu-moe" not in d and "--no-mmproj-offload" not in d and d[d.index("--fit") + 1] == "on",
          f"dense on the card (small file) {d}")
    d0 = fd._llama_args(fd.DENSE_BRAIN, folder, "w.gguf", "p.gguf", False, 1234)
    check("-ngl" in d0 and "--no-mmproj-offload" in d0 and "--device" in d0 and "--fit" not in d0, f"dense from RAM {d0}")


def case_pause():
    fd.LLAMA.paused_for = None
    fd.LLAMA.resume_wanted = False
    fd.LAST_BRAIN = fd.MOE_BRAIN
    frees = []
    fd.COMFY.free = lambda: frees.append(1)
    said = []
    fd._brain_ram_gb = lambda: 19.0
    fd._ram = lambda: (70.0, 64.0)
    fd._room_for_video(said.append)
    check(not fd.LLAMA.paused_for, "plenty of memory: the brain keeps running")
    fd._ram = lambda: (50.0, 64.0)
    fd._room_for_video(said.append)
    check(fd.LLAMA.paused_for == "video" and frees and said, "short of memory: pictures dropped, then the brain pauses")
    check(fd.LLAMA.state() == "paused", "state says paused")
    try:
        fd.LLAMA.ensure(fd.MOE_BRAIN, ([], "w", None), lambda s: None)
        check(False, "chat during the pause must refuse")
    except fd.UserFacing as e:
        check(str(e) == fd.PAUSED_TEXT, "pause text")
    fd.LLAMA.resume()
    check(fd.LLAMA.paused_for is None and fd.LLAMA.state() == "off", "resume lifts it; an idle brain stays off")
    fd.COMFY.free = lambda: None


def case_figures():
    free, total = fd._ram()
    check(free >= 0 and total >= free, "_ram returns (free, total)")
    check(fd.JobQueue().snapshot() == [] and not fd.JobQueue().has_work(), "empty line views")


async def case_timings():
    """11-17: a finished job leaves one memory-only record; the Queue screen's list carries recent + averages."""
    agen = await start()
    fd.TIMINGS.clear()
    a = mk("T1", "s1")
    sub(a)
    await watch(a)
    await asyncio.sleep(0.1)
    recs = list(fd.TIMINGS)
    check(len(recs) == 1 and recs[0].get("total_s") is not None and recs[0].get("ok") is True,
          "a finished job leaves a timing record with its total seconds")
    check(not any("T1" in str(v) for r in recs for v in r.values()), "the record holds no prompt text")
    frames = await op_queue({"action": "list"})
    q = next(f for f in frames if f.get("t") == "info")["queue"]
    check(q.get("recent") and "total_s" in q["recent"][0] and isinstance(q.get("averages"), list),
          "the queue list carries recent jobs and averages")
    fd.TIMINGS.clear()
    await stop(agen)


async def main():
    for name, case in list(globals().items()):
        if name.startswith("case_"):
            if inspect.iscoroutinefunction(case):
                await asyncio.wait_for(case(), timeout=60)
            else:
                case()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{CHECKS} checks, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
