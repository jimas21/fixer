"""Pure-Python checks of Qwen-Image 2.1 (graphs, explicit add-on switch, lineup), the real timings, "Enhance my
prompt" and the ComfyUI cache flags (11-17). Fakes only: no GPU, no ComfyUI, no llama-server, no network, nothing
written under the real data folder.

    set FIXER_COMFY=0 && python desktop\\qwen21_test.py        (any Python with aiohttp + cryptography)

FIXER_HOME is pointed at a throwaway folder (made and deleted here) so no keys or models are read or written.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import types
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="fixer_qwen21_test_")
os.environ["FIXER_HOME"] = TMP
os.environ["FIXER_COMFY"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixer_desktop as fd  # noqa: E402

FAILS: list[str] = []
CHECKS = 0
COMFY_SRC = Path(r"C:\FixerDesktop\comfyui")


def check(cond, msg: str):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


def raises_user_facing(fn, *a, **kw) -> str | None:
    try:
        fn(*a, **kw)
    except fd.UserFacing as e:
        return str(e)
    return None


# ------------------------------------------------------------------ a fake model folder

MODELS = Path(TMP) / "models"
fd.MODELS = MODELS
fd.COMFY_LORAS = MODELS / "comfy-loras"


def snap(repo: str, files: dict | None = None) -> Path:
    d = MODELS / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots" / "abc123"
    d.mkdir(parents=True, exist_ok=True)
    for rel in files or {}:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(b"x")
    return d


def install_21(reader: bool = True, lora: bool = True):
    shutil.rmtree(MODELS, ignore_errors=True)
    snap(fd.QWEN21_REPO, {f"diffusion_models/{fd.QWEN21_UNET}": 1, f"vae/{fd.QWEN21_VAE}": 1})
    if reader:
        snap(fd.QWEN21_TE_REPO, {fd.QWEN21_TE: 1})
    if lora:
        snap(fd.QWEN21_LORA_REPO, {fd.QWEN21_LORA_FILE: 1})


# ------------------------------------------------------------------ graphs

def links(wf: dict):
    """Every ["id", n] input must point at a node that exists."""
    bad = []
    for nid, node in wf.items():
        for k, v in node["inputs"].items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int):
                if v[0] not in wf:
                    bad.append((nid, k, v))
    return bad


def class_known(cls: str) -> bool | None:
    """True if ComfyUI's own source defines the node, None when its folder isn't here."""
    if not COMFY_SRC.is_dir():
        return None
    texts = getattr(class_known, "texts", None)
    if texts is None:
        files = [COMFY_SRC / "nodes.py", COMFY_SRC / "custom_nodes" / "websocket_image_save.py"]
        files += list((COMFY_SRC / "comfy_extras").glob("*.py"))
        texts = class_known.texts = "\n".join(f.read_text(encoding="utf-8", errors="ignore") for f in files if f.is_file())
    return bool(re.search(rf'(node_id\s*=\s*"{cls}"|"{cls}"\s*:|class {cls}\b)', texts))


def case_graphs():
    install_21()
    lora = ("chatrlabs-omni-lora-v0.1-bf16.safetensors", 1.0)
    graphs = {
        "make": fd._qwen21_workflow("a cat", 1024, 1024, 5),
        "make+lora": fd._qwen21_workflow("a cat", 832, 1216, 5, explicit_lora=lora),
        "edit1": fd._qwen21_edit_workflow("make it night", ["a.png"], 5),
        "edit3": fd._qwen21_edit_workflow("photo 2 on photo 1", ["a.png", "b.png", "c.png"], 5, lora),
        "reference": fd._qwen21_edit_workflow("like image 1", ["a.png"], 5, reference_size=(832, 1216)),
        "start": fd._qwen21_workflow("a cat", 1024, 1024, 5, start=("a.png", 0.6, 1000, 700)),
        "style": fd._qwen21_workflow("a cat", 1024, 1024, 5, loras=[("style.safetensors", 0.7)], explicit_lora=lora),
        "warm": fd._qwen21_workflow("warm-up", 64, 64, 0, sigmas="1.0, 0"),
    }
    # paint to change: the crop is image 1 of the editor, only the masked part is redrawn
    saved = fd._qwen21_lora
    fd._qwen21_lora = lambda: lora
    try:
        r = fd.CREATE_RECIPES["qwen21"]
        graphs["paint"] = fd._paint_workflow(r, {"prompt": "a red hat", "explicit": "On"}, 512, 512, 7, [], "crop.png",
                                             "mask.png", 1.0)
    finally:
        fd._qwen21_lora = saved
    for name, wf in graphs.items():
        check(not links(wf), f"{name}: every link points at an existing node ({links(wf)})")
        for nid, node in wf.items():
            ok = class_known(node["class_type"])
            check(ok is not False, f"{name}: node {nid} {node['class_type']} is a real ComfyUI node")
        check(wf["10"]["class_type"] == "SaveImageWebsocket", f"{name}: the picture comes back over the websocket")
        check("rewritten_prompt" not in json.dumps(wf), f"{name}: no prompt-enhancer node or text in the graph")
        check(not any("PreviewAny" in n["class_type"] or "GenerateText" in n["class_type"] or "Switch" in n["class_type"]
                      for n in wf.values()), f"{name}: the official prompt enhancer is left out")
    # make / edit share loader nodes exactly, which is what lets ComfyUI keep them loaded between the two
    for other in ("edit1", "edit3", "reference"):
        for nid in ("1", "2", "3", "7", "8"):
            check(json.dumps(graphs["make"][nid], sort_keys=True) == json.dumps(graphs[other][nid], sort_keys=True),
                  f"node {nid} is identical in make and {other} (no reload)")
    check(graphs["make"]["1"]["inputs"]["unet_name"] == fd.QWEN21_UNET
          and graphs["make"]["2"]["inputs"]["type"] == "qwen_image"
          and graphs["make"]["2"]["inputs"]["clip_name"] == fd.QWEN21_TE
          and graphs["make"]["3"]["inputs"]["vae_name"] == fd.QWEN21_VAE, "the model, reader and VAE names")
    check(graphs["make"]["7"]["inputs"]["sigmas"].count(",") == 8 and graphs["make"]["8"]["inputs"]["sampler_name"] == "euler"
          and graphs["make"]["11"]["inputs"]["cfg"] == 1.0, "Turbo's 8 steps, euler, cfg 1")
    # the add-on: off = not in the graph; on = exactly one, fed by the model and feeding the cache node
    for name in ("make", "edit1", "reference", "start", "warm"):
        check(not any(n["class_type"] == "LoraLoaderModelOnly" for n in graphs[name].values()),
              f"{name}: add-on off means no LoRA node at all")
    for name in ("make+lora", "edit3", "paint"):
        ls = [(k, n) for k, n in graphs[name].items() if n["class_type"] == "LoraLoaderModelOnly"]
        check(len(ls) == 1 and ls[0][1]["inputs"]["model"] == ["1", 0] and graphs[name]["4"]["inputs"]["model"] == [ls[0][0], 0]
              and ls[0][1]["inputs"]["lora_name"] == lora[0] and ls[0][1]["inputs"]["strength_model"] == 1.0,
              f"{name}: exactly one add-on node, after the model and before Turbo's cache node")
    g = graphs["style"]
    check(g["5"]["inputs"]["model"] == ["1", 0] and g["200"]["inputs"]["model"] == ["5", 0]
          and g["4"]["inputs"]["model"] == ["200", 0], "the add-on, then style LoRAs, then the cache node")
    # edit specifics
    e = graphs["edit3"]["20"]["inputs"]
    check(e["images.image_1"] == ["300", 0] and e["images.image_2"] == ["310", 0] and e["images.image_3"] == ["320", 0],
          "up to three photos feed images.image_1..3")
    check("<image2>" in e["prompt"] and "photo 2" not in e["prompt"], "photo N becomes <imageN>")
    check(graphs["edit1"]["12"]["inputs"]["latent_image"] == ["20", 2], "an edit takes its shape from photo 1")
    check(graphs["reference"]["12"]["inputs"]["latent_image"] == ["6", 0]
          and graphs["reference"]["6"]["inputs"]["width"] == 832 and graphs["reference"]["6"]["inputs"]["height"] == 1216,
          "reference mode draws in its own shape")
    # start from a photo
    s = graphs["start"]
    check(s["12"]["inputs"]["sigmas"] == ["14", 1] and s["14"]["inputs"]["denoise"] == 0.6
          and s["6"]["class_type"] == "VAEEncode" and s["41"]["inputs"]["width"] == 992, "start-from keeps the later sigmas")
    # paint
    pt = graphs["paint"]
    check(pt["12"]["inputs"]["latent_image"] == ["6", 0] and pt["6"]["class_type"] == "SetLatentNoiseMask"
          and pt["11"]["inputs"]["model"] == ["44", 0] and pt["44"]["class_type"] == "DifferentialDiffusion"
          and pt["20"]["inputs"]["resolution"] == 0, "paint: noise mask + differential diffusion at the crop's own size")
    # sizes are multiples of 32
    wf = fd._qwen21_workflow("x", 1000, 700, 1)
    check(wf["6"]["inputs"]["width"] % 32 == 0 and wf["6"]["inputs"]["height"] % 32 == 0, "sizes are multiples of 32")


def case_refs():
    check(fd._qwen21_refs("put photo 2 on image 1", 2) == "put <image2> on <image1>", "photo/image N become <imageN>")
    check(fd._qwen21_refs("picture 3 beside Photo 1", 3) == "<image3> beside <image1>", "case-insensitive")
    check(fd._qwen21_refs("picture 4 and photo 3", 3) == "picture 4 and <image3>", "numbers past the photos are left alone")
    check(fd._qwen21_refs("make it night", 1) == "make it night", "other words are untouched")


# ------------------------------------------------------------------ the explicit switch

def case_switch():
    install_21()
    L = ("chatrlabs-omni-lora-v0.1-bf16.safetensors", 1.0)
    ex = fd._qwen21_explicit
    check(ex("Off", True, "zimage-nsfw") is None, "Off never loads it, whatever the brain or model says")
    check(ex("On") == L, "On loads it")
    check(ex("auto", True) == L, "Auto + the brain says explicit: loads")
    check(ex("Auto", False) is None, "Auto + the brain says not explicit: off")
    check(ex("Auto", None, "zimage-nsfw") == L, "Auto + no flag + a picture from an explicit model: loads")
    check(ex("Auto", None, None) is None and ex(None) is None and ex("Auto", None, "chroma") is None,
          "Auto + no flag + nothing explicit: off (never a word list)")
    check(ex(None, True) == L, "chat (no Make switch) follows the brain's flag")
    install_21(lora=False)
    msg = raises_user_facing(ex, "On")
    check(msg and "isn't on your computer yet" in msg and "Off" in msg, "On with no file: plain words, no silent fallback")
    check(raises_user_facing(ex, "Auto", True) is not None, "the brain's yes with no file also says so")
    check(ex("Off", True) is None and ex("Auto", False) is None, "no file but not wanted: fine")
    # live knobs
    install_21()
    cfg = Path(__file__).resolve().parent / "local.json"
    had = cfg.exists()
    check(not had, "the checks need no real local.json (they never touch it)")
    old = fd._local_config
    fd._local_config = lambda: {"qwen21_explicit_strength": 0.8, "qwen21_explicit_lora": "other.safetensors",
                                "qwen21_explicit_repo": "someone/else"}
    try:
        check(fd._qwen21_lora() is None, "a tuned file name that isn't there means the add-on is unavailable")
        snap("someone/else", {"other.safetensors": 1})
        check(fd._qwen21_lora() == ("other.safetensors", 0.8), "file, repo and strength are live-tunable")
    finally:
        fd._local_config = old
    fd._local_config = lambda: {"qwen21_explicit_strength": 9}
    try:
        check(fd._qwen21_lora()[1] == 2.0, "strength is clamped to 2")
    finally:
        fd._local_config = old
    check(fd._tune_text("no_such_key", "dflt") == "dflt", "_tune_text falls back to the default")
    # ready checks
    install_21(reader=False)
    check(not fd._qwen21_ready(), "not ready without a text reader")
    snap(fd.QWEN21_TE_FALLBACK[0], {fd.QWEN21_TE_FALLBACK[1]: 1})
    check(fd._qwen21_ready() and fd._qwen21_te() == fd.QWEN21_TE_FALLBACK[1], "the bf16 reader is used when it is all there is")
    install_21()
    check(fd._qwen21_ready() and fd._qwen21_te() == fd.QWEN21_TE, "the W4A8 reader is preferred")


def case_recipe_and_lineup():
    install_21()
    r = fd.CREATE_RECIPES["qwen21"]
    check(fd._recipe_ready(r), "the 2.1 recipe is ready when its files are here")
    c = {x["key"]: x for x in fd._recipe_controls(r)}
    check("explicit" in c and c["explicit"]["options"] == ["Auto", "On", "Off"] and c["explicit"]["default"] == "Auto",
          "Make gets the Auto / On / Off add-on control")
    check("steps" not in c and "sampler" not in c and "scheduler" not in c and "shift" not in c and "detail" not in c,
          "the fixed 8 sigmas hide steps, sampler, schedule, shift and detail")
    install_21(lora=False)
    c = {x["key"]: x for x in fd._recipe_controls(r)}
    check(c["explicit"]["options"] == ["Off"] and "isn't on your computer yet" in c["explicit"]["help"],
          "no add-on file: only Off, and it says why")
    check(all("explicit" not in {x["key"] for x in fd._recipe_controls(o)}
              for k, o in fd.CREATE_RECIPES.items() if k != "qwen21"), "other recipes have no add-on switch")
    # removed lineup
    check("flux-klein" not in fd.CREATE_RECIPES and "bigasp" not in fd.CREATE_RECIPES, "FLUX klein and bigASP recipes are gone")
    check(set(fd.CREATE_RECIPES) == {"zimage", "zimage-nsfw", "chroma", "qwen21"}, "the lineup is Z-Image (+NSFW), Chroma, 2.1")
    check(all("bigasp" not in k.lower() and "flux.2" not in k.lower() for k in fd.COMFY_RECIPES), "no removed picture-model ids")
    check(fd.COMFY_RECIPES["Comfy-Org/Qwen-Image-2.1"] == "qwen21", "the phone's 2.1 model id maps to its recipe")
    check(fd.BRAIN_ALIASES[fd.DENSE_BRAIN] == fd.MOE_BRAIN, "the 27B maps to the MoE brain")
    a = fd.AUTO_MODELS_DEFAULT
    check(all(x in fd.CREATE_RECIPES for v in a.values() if isinstance(v, list) for x in v), "auto lists name only real recipes")
    check("qwen21" not in json.dumps(a), "defaults do not change: 2.1 is not in the auto lists")
    cfg = json.loads((Path(__file__).resolve().parent / "phone_config.json").read_text(encoding="utf-8"))
    am = cfg["auto_models"]
    check(all(x in fd.CREATE_RECIPES for v in am.values() if isinstance(v, list) for x in v), "phone_config auto_models are real recipes")
    check('"explicit"' in cfg["photo_tools"] and cfg["photo_tools"].count('"explicit": false') == 3
          and 'never changes the words' in cfg["photo_tools"], "photo_tools describes the explicit flag on make, redraw, video")
    src = Path(fd.__file__).read_text(encoding="utf-8")
    live = [ln for ln in src.splitlines() if re.search(r"flux2|_sdxl|bigasp|flux-klein|FALLBACK_IMAGE|DEFAULT_IMAGE", ln)
            and not ln.lstrip().startswith("#")]
    check(not live, f"no code is left that points at removed models ({live[:3]})")
    check(not re.search(r'"(black-forest-labs/FLUX|GraydientPlatformAPI)', src), "nothing names a removed model repo")
    # the chat's saved choices that are gone fall through to Auto
    fd._recipe_ready_saved = fd._recipe_ready
    fd._recipe_ready = lambda r_: True
    try:
        check(fd._picture_recipe({"model_choice": "bigasp", "prompt": "a cat"}) is fd.CREATE_RECIPES["zimage"],
              "a saved choice that is gone (bigASP) falls through to Auto")
    finally:
        fd._recipe_ready = fd._recipe_ready_saved
    # chat: the brain's flag, else the words (old phones)
    fd._recipe_ready_saved = fd._recipe_ready
    fd._recipe_ready = lambda r_: True
    try:
        check(fd._picture_recipe({"prompt": "a cat", "explicit": True}) is fd.CREATE_RECIPES["chroma"],
              "the brain's explicit true picks the explicit list")
        check(fd._picture_recipe({"prompt": "a naked woman", "explicit": False}) is fd.CREATE_RECIPES["chroma"],
              "the brain's explicit false means not said: the word check still routes explicit words")
        check(fd._picture_recipe({"prompt": "a cat", "explicit": False}) is fd.CREATE_RECIPES["zimage"],
              "explicit false and plain words stay on the clean model")
        check(fd._picture_recipe({"prompt": "a naked woman"}) is fd.CREATE_RECIPES["chroma"],
              "an old phone (no flag) keeps the word check")
        check(fd._picture_recipe({"prompt": "x", "model_choice": "qwen21"}) is fd.CREATE_RECIPES["qwen21"],
              "the chat can choose 2.1")
    finally:
        fd._recipe_ready = fd._recipe_ready_saved


def case_paths_yaml():
    install_21()
    snap("black-forest-labs/FLUX.2-klein-9B", {"x.safetensors": 1})      # a leftover must not be listed any more
    y = fd._comfy_paths_yaml()
    q = (MODELS / "hub" / f"models--{fd.QWEN21_REPO.replace('/', '--')}" / "snapshots" / "abc123").as_posix()
    check(f"{q}/diffusion_models" in y and f"{q}/vae" in y, "the 2.1 model and VAE folders are listed")
    check(fd._snapshot(fd.QWEN21_TE_REPO).as_posix() in y, "the 2.1 text reader folder is listed")
    check(fd._snapshot(fd.QWEN21_LORA_REPO).as_posix() in y and "loras:" in y, "the add-on's folder is listed under loras")
    check("flux" not in y.lower() and "sdxl" not in y.lower() and "diffusers:" not in y, "no FLUX / SDXL / diffusers entries")
    z = snap("Comfy-Org/Z-Image-Turbo", {"split_files/vae/ae.safetensors": 1})
    check((z / "split_files" / "vae").as_posix() in fd._comfy_paths_yaml(), "ae.safetensors (Chroma needs it) stays")


# ------------------------------------------------------------------ making and editing through the real code paths

class FakeRun:
    def __init__(self):
        self.graphs: list[dict] = []

    async def __call__(self, wf, emit, cancel, previews=False, frames=False, label="x"):
        self.graphs.append(wf)
        return b"PNG"


def case_pictures():
    install_21()
    run = FakeRun()
    saved = (fd._comfy_run, fd._qwen_edit_ready, fd._qwen_explicit)
    fd._comfy_run = run
    cancel = threading.Event()
    out: list[dict] = []
    emit = out.append
    r21, rz = fd.CREATE_RECIPES["qwen21"], fd.CREATE_RECIPES["zimage"]
    fd._TL.rec = fd._new_timing_record("picture")
    try:
        # a new 2.1 picture: add-on Off / Auto = clean base, On = the add-on node
        v = {**r21["defaults"], "prompt": "a cat"}
        fd._create_pictures(r21, {**v, "explicit": "Auto"}, 1024, 1024, 1, 1, [], None, None, "start", emit, cancel,
                            lambda s: None, (), "qwen21")
        check("5" not in run.graphs[-1], "Make, add-on Auto, new picture: the clean base")
        fd._create_pictures(r21, {**v, "explicit": "On"}, 1024, 1024, 1, 1, [], None, None, "start", emit, cancel,
                            lambda s: None, (), "qwen21")
        check(run.graphs[-1]["5"]["class_type"] == "LoraLoaderModelOnly" and fd._TL.rec["explicit"] is True,
              "Make, add-on On: the add-on is in the graph and the timing says so")
        check(fd._TL.rec["model"] == fd.QWEN21_NAME and fd._TL.rec["steps"] == 8 and abs(fd._TL.rec["mp"] - 1.048576) < 1e-6,
              "the timing note carries model, steps and megapixels")
        # an edit of a 2.1 picture goes to the 2.1 editor (same model: no reload); the switch carries over
        from PIL import Image
        photo = Image.new("RGB", (800, 600))
        fd._create_pictures(r21, {**v, "explicit": "On", "image_mode": "edit", "prompt": "make it night"}, 1024, 1024, 1,
                            1, [], photo, "p.png", "edit", emit, cancel, lambda s: None, ("x.png",), "qwen21")
        g = run.graphs[-1]
        check(g["20"]["class_type"] == "TextEncodeQwenImage21" and g["20"]["inputs"]["images.image_2"] == ["310", 0]
              and "5" in g, "Auto editor on a 2.1 picture: the 2.1 editor, two photos, the add-on that was chosen")
        check(all(json.dumps(g[k], sort_keys=True) == json.dumps(run.graphs[1][k], sort_keys=True) for k in ("1", "2", "3")),
              "the edit's loader nodes equal the make's (the cache keeps them loaded)")
        # an edit of any other picture stays on the 2511 / Rapid-AIO editors
        fd._qwen_edit_ready = lambda explicit=False: True
        fd._qwen_explicit = lambda prompt, recipe_id=None: False
        fd._create_pictures(rz, {**rz["defaults"], "image_mode": "edit", "prompt": "make it night"}, 1024, 1024, 1, 1,
                            [], photo, "p.png", "edit", emit, cancel, lambda s: None, (), "zimage")
        check(any(n["class_type"] == "TextEncodeQwenImageEditPlus" for n in run.graphs[-1].values()),
              "defaults do not change: a Z-Image picture is still edited by Qwen-Image-Edit 2511")
        fd._create_pictures(rz, {**rz["defaults"], "image_mode": "edit", "editor": "qwen21", "prompt": "x"}, 1024, 1024,
                            1, 1, [], photo, "p.png", "edit", emit, cancel, lambda s: None, (), "zimage")
        check("TextEncodeQwenImage21" in {n["class_type"] for n in run.graphs[-1].values()}, "the editor menu can choose 2.1")
        fd._create_pictures(rz, {**rz["defaults"], "image_mode": "edit", "editor": "flux", "prompt": "x"}, 1024, 1024,
                            1, 1, [], photo, "p.png", "edit", emit, cancel, lambda s: None, (), "zimage")
        check(any(n["class_type"] == "TextEncodeQwenImageEditPlus" for n in run.graphs[-1].values()),
              "an old phone's 'flux' editor choice is treated as Auto")
        # explicit zimage-nsfw picture edited with the 2.1 editor: Auto follows the explicit model
        fd._create_pictures(fd.CREATE_RECIPES["zimage-nsfw"], {**rz["defaults"], "image_mode": "edit", "editor": "qwen21",
                                                               "prompt": "x"}, 1024, 1024, 1, 1, [], photo, "p.png", "edit",
                            emit, cancel, lambda s: None, (), "zimage-nsfw")
        check("5" in run.graphs[-1], "Auto add-on + a picture from the explicit model: the add-on loads")
        # no add-on file: On says so and nothing is drawn
        install_21(lora=False)
        n = len(run.graphs)
        msg = raises_user_facing(fd._create_pictures, r21, {**v, "explicit": "On"}, 1024, 1024, 1, 1, [], None, None,
                                 "start", emit, cancel, lambda s: None, (), "qwen21")
        check(msg and len(run.graphs) == n, "On without the add-on file: plain words, nothing drawn")
        # no editor at all
        fd._qwen_edit_ready = lambda explicit=False: False
        install_21()
        msg = raises_user_facing(fd._create_pictures, rz, {**rz["defaults"], "image_mode": "edit", "prompt": "x"}, 1024,
                                 1024, 1, 1, [], photo, "p.png", "edit", emit, cancel, lambda s: None, (), "zimage")
        check(msg and "photo editor model" in msg, "no photo editor model: plain words")
    finally:
        fd._comfy_run, fd._qwen_edit_ready, fd._qwen_explicit = saved
        fd._TL.rec = None


def case_chat():
    install_21()
    run = FakeRun()
    saved = (fd._comfy_run, fd._picture_recipe, fd.COMFY.ensure, fd.COMFY.ensure_current, fd._room_for_pictures,
             fd._worker_alive, fd._qwen_edit_ready)
    fd._comfy_run = run
    fd.COMFY.ensure = fd.COMFY.ensure_current = lambda status=None: None
    fd._room_for_pictures = lambda: None
    fd._worker_alive = lambda: False
    r21 = fd.CREATE_RECIPES["qwen21"]
    fd._picture_recipe = lambda p: r21
    out: list[dict] = []
    fd._TL.rec = fd._new_timing_record("picture")
    try:
        fd.job_comfy_image({"prompt": "a cat", "width": 1024, "height": 1024, "explicit": True}, out.append, threading.Event())
        img = [f for f in out if f.get("t") == "image"][-1]
        check("5" in run.graphs[-1] and img["recipe"] == "qwen21" and img["explicit"] is True,
              "chat: the brain's explicit flag loads the add-on and the result frame says which recipe and add-on")
        fd.job_comfy_image({"prompt": "a cat", "width": 1024, "height": 1024}, out.append, threading.Event())
        check("5" not in run.graphs[-1], "an old phone (no flag) never loads the add-on")
        # chat edit with the 2.1 editor
        from PIL import Image
        import base64
        import io
        buf = io.BytesIO()
        Image.new("RGB", (64, 48)).save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        fd.job_comfy_edit({"image": b64, "prompt": "make it night", "model_choice": "qwen21", "explicit": False},
                          out.append, threading.Event())
        g = run.graphs[-1]
        check(g["20"]["class_type"] == "TextEncodeQwenImage21" and "5" not in g, "chat edit with 2.1 and explicit false: clean base")
        fd.job_comfy_edit({"image": b64, "prompt": "make it night", "model_choice": "qwen21", "explicit": True},
                          out.append, threading.Event())
        check("5" in run.graphs[-1], "chat edit with 2.1 and explicit true: the add-on")
        fd._qwen_edit_ready = lambda explicit=False: False
        msg = raises_user_facing(fd.job_comfy_edit, {"image": b64, "prompt": "x"}, out.append, threading.Event())
        check(msg and "photo editor model" in msg, "no editor: plain words (the FLUX fallback is gone)")
        check(fd._comfy_edits_ready.__code__.co_names.count("_qwen21_ready") == 1, "2.1 counts as a photo editor")
    finally:
        (fd._comfy_run, fd._picture_recipe, fd.COMFY.ensure, fd.COMFY.ensure_current, fd._room_for_pictures,
         fd._worker_alive, fd._qwen_edit_ready) = saved
        fd._TL.rec = None


# ------------------------------------------------------------------ timings

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def case_timing_watch():
    wf = fd._qwen21_workflow("a cat", 1024, 1024, 1)
    clk = Clock()
    w = fd._TimingWatch(wf, clk)
    clk.t = 100.5
    w.on("execution_start", {"prompt_id": "x"})
    w.on("execution_cached", {"nodes": []})                  # nothing cached: ComfyUI loads everything
    clk.t = 108.5
    w.on("progress", {"node": "20", "value": 1, "max": 8})   # the text reader: not a sampler
    clk.t = 110.5
    w.on("progress", {"node": "12", "value": 1, "max": 8})   # first sampler step
    clk.t = 112.0
    w.on("progress", {"node": "12", "value": 2, "max": 8})
    clk.t = 115.5
    w.on("execution_success", {"prompt_id": "x"})
    x = w.result()
    check(x["load_s"] == 10.0 and x["draw_s"] == 5.0 and x["loaded"] is True,
          "load/draw split at the first sampler step; nothing cached means it loaded")
    # everything cached: no load
    clk = Clock()
    w = fd._TimingWatch(wf, clk)
    w.on("execution_start", {})
    w.on("execution_cached", {"nodes": ["1", "2", "3", "7", "8"]})
    clk.t = 101.0
    w.on("progress", {"node": "12"})
    clk.t = 106.0
    w.on("executing", {"node": None})
    x = w.result()
    check(x["loaded"] is False and x["load_s"] == 1.0 and x["draw_s"] == 5.0, "all loaders cached: loaded is False")
    # one loader missing from the cache (a LoRA was just switched on)
    w = fd._TimingWatch(fd._qwen21_workflow("a", 256, 256, 1, explicit_lora=("l.safetensors", 1.0)), clk)
    w.on("execution_cached", {"nodes": ["1", "2", "3"]})
    check(w.result()["loaded"] is True, "the add-on's loader not cached counts as a load")
    # a cached identical prompt: no sampler progress
    clk = Clock()
    w = fd._TimingWatch(wf, clk)
    w.on("execution_start", {})
    w.on("execution_cached", {"nodes": list(wf)})
    clk.t = 100.2
    w.on("execution_success", {})
    x = w.result()
    check(x["load_s"] == 0.0 and abs(x["draw_s"] - 0.2) < 1e-9 and x["loaded"] is False, "no sampler progress: all drawing")
    # video-style graphs with two samplers, and apply() adds runs up
    fd._TL.rec = fd._new_timing_record("video")
    for _ in range(2):
        clk = Clock()
        w = fd._TimingWatch({"4": {"class_type": "UNETLoader", "inputs": {}}, "13": {"class_type": "KSamplerAdvanced", "inputs": {}}}, clk)
        w.on("execution_start", {})
        clk.t = 101.0
        w.on("progress", {"node": "13"})
        clk.t = 103.0
        w.on("execution_success", {})
        w.apply()
    r = fd._TL.rec
    check(r["runs"] == 2 and r["load_s"] == 2.0 and r["draw_s"] == 4.0 and r["loaded"] is True, "apply() adds runs together")
    fd._TL.rec = None
    w.apply()                                                  # outside a job: a no-op
    check(True, "apply() outside a job does nothing")


def rec(model, kind, total, draw, loaded, explicit=False, mp=1.0, steps=8, ok=True, at=1.0, count=1):
    r = fd._new_timing_record(kind)
    r.update({"model": model, "total_s": total, "draw_s": draw, "load_s": total - draw, "loaded": loaded, "mp": mp,
              "steps": steps, "explicit": explicit, "ok": ok, "at": at, "count": count})
    return r


def case_timing_summary():
    fd.TIMINGS.clear()
    for i in range(130):
        fd.TIMINGS.append(rec("m", "picture", 10, 8, False, at=float(i)))
    check(len(fd.TIMINGS) == 100, "only the last 100 jobs are kept")
    fd.TIMINGS.clear()
    fd.TIMINGS.append(rec("Qwen-Image 2.1 Turbo", "picture", 8.0, 6.4, False, at=1.0))
    fd.TIMINGS.append(rec("Qwen-Image 2.1 Turbo", "pictures", 10.0, 8.0, False, at=2.0))
    fd.TIMINGS.append(rec("Qwen-Image 2.1 Turbo", "picture", 32.0, 8.0, True, at=3.0))
    fd.TIMINGS.append(rec("Qwen-Image 2.1 Turbo", "picture", 99.0, 50.0, True, ok=False, at=4.0))   # failed: not averaged
    fd.TIMINGS.append(rec("Qwen-Image 2.1 Turbo", "picture", 12.0, 9.0, False, explicit=True, at=5.0))
    fd.TIMINGS.append(rec("Chroma1-HD", "picture", 41.0, 36.0, False, steps=26, at=0.5))
    fd.TIMINGS.append(rec("Qwen Edit 2511", "edit", 20.0, 15.0, False, steps=4, at=0.6))
    fd.TIMINGS.append(rec("Qwen Edit 2511", "change", 22.0, 17.0, False, steps=4, at=0.7))
    s = fd._timing_summary()
    by = {(g["model"], g["kind"], g["explicit"]): g for g in s}
    g = by[("Qwen-Image 2.1 Turbo", "new picture", False)]
    check(g["n"] == 3 and abs(g["total_s"] - 16.67) < 0.01 and g["warm_total_s"] == 9.0 and g["cold_total_s"] == 32.0,
          "new pictures group together; the failed job is left out; warm vs cold averages")
    check(g["s_per_mp_step"] and abs(g["s_per_mp_step"] - 0.9) < 0.1, "seconds per megapixel-step")
    check(("Qwen-Image 2.1 Turbo", "new picture", True) in by, "add-on on and off are separate rows")
    check(by[("Qwen Edit 2511", "edit", False)]["n"] == 1 and by[("Qwen Edit 2511", "change", False)]["n"] == 1,
          "a paint change is its own kind, apart from full-photo edits")
    check(s[0]["model"] == "Qwen-Image 2.1 Turbo" and s[0]["explicit"] is True, "most recently used first")
    recent = fd._timing_recent(3)
    check(len(recent) == 3 and recent[0]["model"] == "Qwen Edit 2511" and "age_s" in recent[0] and "at" not in recent[0],
          "recent jobs come back newest first with an age")
    fd.TIMINGS.append(rec("Per", "pictures", 40.0, 32.0, False, at=9.0, count=4))
    fd.TIMINGS.append(rec("Per", "picture", 10.0, 8.0, False, at=9.5))
    gp = {(g["model"], g["kind"]): g for g in fd._timing_summary()}[("Per", "new picture")]
    check(abs(gp["total_s"] - 10.0) < 0.01, "a 4-picture job averages per picture")
    text = "\n".join(fd._measured_speed_facts())
    check("Qwen-Image 2.1 Turbo, new picture: 17 s on average over 3" in text and "9.0 s when already loaded" in text
          and "32 s when it had to load" in text and "explicit add-on on" in text, "the look-up lines read in plain words")
    check("RECENT FINISHED JOBS" in "\n".join(fd._recent_jobs_facts(2)), "the jobs facts list recent jobs")
    check(not any("prompt" in k or "words" in k for r in fd.TIMINGS for k in r), "records hold no prompt text")
    fd.TIMINGS.clear()


def case_record_timing():
    fd.TIMINGS.clear()
    job = fd.Job("image", {"prompt": "secret words", "slot": "s"}, "p")
    job.started = fd.time.monotonic() - 3.0
    r = fd._new_timing_record("picture")
    r["model"] = "m"
    fd._record_timing(job, r, None)
    check(len(fd.TIMINGS) == 1 and 2.9 < fd.TIMINGS[0]["total_s"] < 4 and fd.TIMINGS[0]["ok"] is True, "a finished job is recorded")
    check("secret words" not in json.dumps(list(fd.TIMINGS)), "no prompt text in the record")
    fd._record_timing(job, fd._new_timing_record("picture"), fd.UserFacing("boom"))
    check(fd.TIMINGS[-1]["ok"] is False, "a failed job is kept, marked failed")
    job.stop_reason = "cancelled"
    n = len(fd.TIMINGS)
    fd._record_timing(job, fd._new_timing_record("picture"), None)
    check(len(fd.TIMINGS) == n, "a stopped job is not recorded")
    warm = fd.Job("warm", {"for": "make"}, "p")
    warm.started = fd.time.monotonic()
    fd._record_timing(warm, fd._new_timing_record("warm"), None)
    check(len(fd.TIMINGS) == n, "warm-ups are not recorded")
    fd.TIMINGS.clear()


# ------------------------------------------------------------------ Enhance my prompt

class FakeLoop:
    def call_soon_threadsafe(self, fn, *a):
        fn(*a)


class Sub:
    def __init__(self):
        self.items = []

    def put_nowait(self, o):
        self.items.append(o)


def case_enhance_text():
    c = fd._clean_enhanced
    check(c('{"rewritten_prompt": "A woman in a red coat, rain.", "wh_ratio": "3:2"}') == "A woman in a red coat, rain.",
          "the JSON wrapper is stripped and the aspect ratio ignored")
    check(c('```json\n{"rewritten_prompt": "X \\"quoted\\" y"}\n```') == 'X "quoted" y', "a code fence and escapes are handled")
    check(c('<think>hmm</think>\n"A long plain prompt."') == "A long plain prompt.", "a think block and surrounding quotes go")
    check(c('{"rewritten_prompt": "cut off') == "", "a broken JSON wrapper gives nothing rather than half a prompt")
    check(c("Just the prompt.") == "Just the prompt.", "a plain reply passes")
    # which instruction file is fetched, and the rules that always follow
    asked = []
    saved = fd._fetch_text
    fd._ENHANCE_TEXT.clear()

    def fetch(repo, name):
        asked.append((repo, name))
        return "QWEN INSTRUCTIONS " + repo
    fd._fetch_text = fetch
    try:
        t = fd._enhance_system(False)
        e = fd._enhance_system(True)
        check(t.startswith("QWEN INSTRUCTIONS Qwen/Qwen-Image-2.1-PE-T2I") and e.startswith("QWEN INSTRUCTIONS Qwen/Qwen-Image-2.1-PE-I2I"),
              "text-to-image and edit use their own published instruction file")
        check(asked == [("Qwen/Qwen-Image-2.1-PE-T2I", "system_prompt.txt"), ("Qwen/Qwen-Image-2.1-PE-I2I", "system_prompt.txt")],
              "fetched from the model cache by name (never copied into the repo)")
        for s in (t, e):
            check("Never soften" in s and "never add clothing" in s and "exactly as written" in s and "English" in s
                  and "aspect ratio" in s, "the owner's rules follow, and win")
        fd._enhance_system(False)
        check(len(asked) == 2, "a fetched instruction is kept for next time")

        def boom(repo, name):
            raise OSError("offline")
        fd._ENHANCE_TEXT.clear()
        fd._fetch_text = boom
        b = fd._enhance_system(False)
        check(b.startswith(fd.ENHANCE_BUILTIN_T2I) and fd.ENHANCE_RULES in b, "no fetch: a short built-in instruction plus the rules")
        check(fd._enhance_system(True).startswith(fd.ENHANCE_BUILTIN_I2I), "the built-in edit instruction only sharpens the request")
    finally:
        fd._fetch_text = saved
        fd._ENHANCE_TEXT.clear()


def case_enhance_run():
    saved = (fd._enhance_port, fd._chat_llama, fd._fetch_text)
    seen = {}
    reply = {"text": "A woman in a red coat stands in heavy rain on a neon-lit street, shallow depth of field."}

    def fake_llama(p, emit, cancel, port):
        seen.update(p=p, port=port)
        for piece in (reply["text"][:10], reply["text"][10:]):
            emit({"t": "text", "v": piece})
    fd._chat_llama = fake_llama
    fd._fetch_text = lambda repo, name: ""
    fd._enhance_port = lambda: 4321
    try:
        cancel = threading.Event()
        words = "woman in a red coat in the rain"
        text, note = fd.enhance_prompt(words, False, None, cancel)
        check(text == reply["text"] and note == "", "the brain's improved prompt comes back")
        p = seen["p"]
        check(seen["port"] == 4321 and p["think"] is False and p["messages"][0]["text"] == words
              and "image" not in p["messages"][0], "Think off, the user's own words sent as they are")
        text, note = fd.enhance_prompt("make it night", True, "B64PHOTO", cancel)
        check(seen["p"]["messages"][0].get("image") == "B64PHOTO", "an edit sends the photo to the brain")
        reply["text"] = "short"
        text, note = fd.enhance_prompt("a woman in a long red coat in the rain at night", False, None, cancel)
        check(text is None and "your own words" in note, "a reply that drops most of the request is not used")
        fd._enhance_port = lambda: None
        text, note = fd.enhance_prompt("a cat", False, None, cancel)
        check(text is None and "isn't available" in note and "your own words" in note, "brain off or paused: say so in plain words")

        def broken():
            raise fd.UserFacing("paused")
        fd._enhance_port = broken
        text, note = fd.enhance_prompt("a cat", False, None, cancel)
        check(text is None and note, "a brain that can't start is a plain note, not an error")
        fd._enhance_port = lambda: 1
        fd._chat_llama = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        text, note = fd.enhance_prompt("a cat", False, None, cancel)
        check(text is None and note, "a failing brain call is a plain note, not an error")
    finally:
        fd._enhance_port, fd._chat_llama, fd._fetch_text = saved


def case_enhance_job():
    inp = fd._enhance_inputs
    check(inp("comfy_run", {"values": {"prompt": "a", "enhance": True}}) == ("a", False, None), "Make: a new picture is enhanced")
    check(inp("comfy_run", {"values": {"prompt": "a"}}) is None and inp("image", {"prompt": "a"}) is None,
          "no flag (old phones) means off")
    check(inp("comfy_run", {"values": {"prompt": "a", "enhance": True, "image_mode": "paint", "change": "x"}}) is None,
          "paint (a spot's own words) is never enhanced")
    check(inp("comfy_run", {"values": {"prompt": "a", "enhance": True, "image": "P", "image_mode": "edit"}}) == ("a", True, "P"),
          "Make: an edit uses the edit instruction and its photo")
    check(inp("image", {"prompt": "a", "enhance": True, "image": "P"}) == ("a", True, "P")
          and inp("image", {"prompt": "a", "enhance": True}) == ("a", False, None), "chat: edit vs new picture")
    check(inp("image", {"prompt": "a", "enhance": "yes"}) is None, "only a real true switches it on")
    saved = (fd._enhance_port, fd._chat_llama, fd._fetch_text)
    fd._enhance_port = lambda: 1
    fd._fetch_text = lambda repo, name: ""
    fd._chat_llama = lambda p, emit, cancel, port: emit({"t": "text", "v": "A much longer and richer description of a cat."})
    try:
        payload = {"slot": "s", "values": {"prompt": "a cat", "enhance": True, "shape": "square"}}
        job = fd.Job("comfy_run", payload, "p")
        check(job.enhance and not job.enhance_done.is_set(), "a job that asks for it waits for it")
        job.loop = FakeLoop()
        sub = Sub()
        job.subs.add(sub)
        fd._enhance_job(job)
        check(job.enhanced and job.enhance_state == "on" and job.enhance_done.is_set() and job.enhance_s >= 0,
              "the improved prompt is stored with seconds and on/off for the timings")
        check({"t": "enhanced", "v": job.enhanced} in sub.items, "an enhanced frame goes out before drawing")
        got = []
        out = fd._apply_enhance(job, payload, got.append)
        check(out["values"]["prompt"] == job.enhanced and payload["values"]["prompt"] == "a cat"
              and out["values"]["shape"] == "square" and job.words == "a cat", "drawn from the improved words; the original is kept")
        # unavailable: original words and a plain note
        fd._enhance_port = lambda: None
        job2 = fd.Job("image", {"prompt": "a cat", "enhance": True, "slot": "s"}, "p")
        job2.loop = FakeLoop()
        sub2 = Sub()
        job2.subs.add(sub2)
        fd._enhance_job(job2)
        got = []
        out2 = fd._apply_enhance(job2, job2.payload, got.append)
        check(out2 is job2.payload and job2.enhance_state == "failed" and any("your own words" in (g.get("v") or "") for g in got)
              and any("enhance_skipped" in i for i in sub2.items), "no brain: the original words are drawn and the phone is told")
        # a job that never asked
        job3 = fd.Job("image", {"prompt": "a cat", "slot": "s"}, "p")
        check(not job3.enhance and job3.enhance_done.is_set() and fd._apply_enhance(job3, job3.payload, got.append) is job3.payload,
              "no flag: nothing waits and nothing changes")
        # cancelled while waiting
        job4 = fd.Job("image", {"prompt": "a cat", "enhance": True, "slot": "s"}, "p")
        job4.cancel.set()
        check(fd._apply_enhance(job4, job4.payload, got.append) is job4.payload, "a stopped job does not wait for the brain")
    finally:
        fd._enhance_port, fd._chat_llama, fd._fetch_text = saved


# ------------------------------------------------------------------ ComfyUI start flags

def case_cache_flags():
    install_21()
    seen = {}

    class P:
        pid = 1

        def poll(self):
            return None

    def popen(args, **kw):
        seen["args"] = args
        return P()
    fd._timing_saved = None
    saved = (fd.subprocess.Popen, fd._JOB, fd.COMFY._get, fd.COMFY_DIR, fd.COMFY_PRIVATE)
    fd.subprocess.Popen = popen
    fd._JOB = types.SimpleNamespace(add=lambda p: None)
    fd.COMFY._get = lambda path, timeout=3: {}
    fd.COMFY_PRIVATE = Path(TMP) / "comfy-private"
    fd._TL.rec = fd._new_timing_record("picture")
    try:
        fd.COMFY.proc = None
        fd.COMFY.ensure()
        a = seen["args"]
        i = a.index("--cache-ram")
        check(a[i + 1:i + 3] == ["3", "6"], "ComfyUI starts with --cache-ram 3 6 (models stay cached in RAM)")
        check("--high-ram" not in a and "--cache-classic" not in a and "--fast" not in a and "--disable-smart-memory" not in a,
              "no flags that waste RAM or cost quality")
        check("engine_start_s" in {k for k, v in fd._TL.rec.items() if v is not None} and fd._TL.rec["engine_start_s"] >= 0,
              "the engine start time is noted")
        local = fd._local_config
        fd._local_config = lambda: {"comfy_cache_inactive_gb": 10.5, "comfy_cache_active_gb": 2}
        fd.COMFY.proc = None
        fd.COMFY.ensure()
        a = seen["args"]
        check(a[a.index("--cache-ram") + 1:a.index("--cache-ram") + 3] == ["2", "10.5"], "the cache thresholds are live knobs")
        fd._local_config = local
    finally:
        fd.subprocess.Popen, fd._JOB, fd.COMFY._get, fd.COMFY_DIR, fd.COMFY_PRIVATE = saved
        fd.COMFY.proc = None
        fd._TL.rec = None


def case_queue_list():
    fd.TIMINGS.clear()
    fd.TIMINGS.append(rec("Qwen-Image 2.1 Turbo", "picture", 8.0, 6.4, False))
    frames: list[dict] = []

    async def send(obj):
        frames.append(obj)
    figs = {"card": {"name": "c", "used_gb": 1, "total_gb": 16}, "memory": {"used_gb": 1, "total_gb": 64}}
    saved = fd._figures
    fd._figures = lambda: figs
    try:
        asyncio.run(fd._op_queue({"action": "list"}, send, "p"))
    finally:
        fd._figures = saved
    q = frames[0]["queue"]
    check(q["recent"][0]["model"] == "Qwen-Image 2.1 Turbo" and q["recent"][0]["total_s"] == 8.0
          and q["averages"][0]["n"] == 1 and "jobs" in q and "brain" in q, "the queue list carries recent jobs and averages")
    fd.TIMINGS.clear()


if __name__ == "__main__":
    try:
        for name, case in list(globals().items()):
            if name.startswith("case_"):
                case()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{CHECKS} checks, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
