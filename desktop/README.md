# Fixer Desktop

The computer half of Fixer. When **Use my computer** is on in the app, chat runs on a larger vision model and
images are made and edited on the computer's graphics card. Remove, cut out and upscale stay on the phone.

## Install

In PowerShell on the computer with the NVIDIA card:

```
irm https://raw.githubusercontent.com/jimas21/fixer/main/desktop/install.ps1 | iex
```

It installs into `C:\FixerDesktop`: Python 3.12 and the server's libraries exactly as locked in
`requirements-lock.txt`, llama.cpp b12443 for chat, ComfyUI v0.45.5 (`comfyui-lock.txt`) for pictures, video and songs,
and the tray at login, then starts it. About 25 GB; models are fetched later, from the phone. Re-run it to repair or
update. Then pair: right-click the Fixer icon by the clock, **Pair a phone**, and scan the code in the app.

Your styles (`recipes.json`) are private and are not in this repo; a fresh install starts without them.

## Remove

From the phone: **Settings → Remove Fixer everywhere**. The computer uninstalls Fixer Desktop and the iPhone wipes
everything Fixer holds; then remove the app's icon (iOS apps can't delete themselves). Nothing is kept on either side.

On the computer alone: `powershell -ExecutionPolicy Bypass -File C:\FixerDesktop\uninstall.ps1`. It stops the tray
and server, removes the Startup shortcut and deletes `C:\FixerDesktop` with everything in it (models, styles,
settings, pairings). Shared tools (uv, its cache and its Python) are left alone.

## What it keeps

Nothing about a request. Requests and results exist in memory only and are dropped when the reply is sent:
no uploads or outputs folder, no content logs, no history. The owner's rules live on the phone and travel with
each request. On disk there is only:

| Path | What |
|---|---|
| `C:\FixerDesktop\venv\` | Python and its libraries |
| `C:\FixerDesktop\llama\` | llama.cpp (runs the chat model) |
| `C:\FixerDesktop\models\` | downloaded models, capped at 200 GB (nothing is removed automatically: the phone shows which models are here and removes them) |
| `C:\FixerDesktop\keys.json` | one random key per paired phone |
| `C:\FixerDesktop\tray.json` | the tray's own two settings |
| `C:\FixerDesktop\*.py` | this code |

`trace_test.py` proves it: it sends a chat with a photo, an image edit and a search, each carrying a unique
marker, then searches every file that changed on the computer for the marker, the photo and the result.

## How it uses the computer

- **Chat** runs on [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server`, started on the first chat
  and kept running. It listens on 127.0.0.1 only, with no web UI, no logs and no saved slots. The model stays
  loaded (about 7–8 GB of graphics memory), so answers start immediately.
- **Images** run on diffusers. The image model waits in RAM and moves to the graphics card part by part while it
  draws; after 5 minutes without a request it is unloaded.
- Everything runs at below-normal priority, one request at a time. **Free graphics memory** in the tray menu
  unloads both models (they reload on the next request); **Turn off** stops the server entirely.

Settings (environment variables): `FIXER_IDLE_MINUTES` (5), `FIXER_MODEL_CAP_GB` (200), `FIXER_PORT` (8765),
`FIXER_HOME` (`C:\FixerDesktop`), `FIXER_BRAIN_VRAM_GB` (7, budget for chat weights + vision projector),
`FIXER_BRAIN_CTX` (16384).

## Security

Each phone has its own 256-bit key (made by `pair.py`, kept in `keys.json` and the phone's keychain). Every
request and every reply frame is AES-256-GCM sealed; the timestamp, phone id and frame number are
authenticated, requests older than 2 minutes or seen before are refused, and anything that doesn't decrypt gets a
bare 401. `pair.py --remove <id>` cuts a phone off at once.

## Models

Chat (the brain): `llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-GGUF`, a mixture of experts: the expert
weights stay in system memory and only the small attention part sits on the card, so it stays loaded beside the picture
engine. Any other GGUF repo on Hugging Face still works (the server picks the best quant that fits the budget and the
repo's `mmproj` file for photos). The old 27B model is gone; a phone that still asks for it gets the 35B-A3B.

Pictures and photo edits run in ComfyUI (own venv, `127.0.0.1` only):

- **Juggernaut XL Ragnarok** (`Kandoo/Juggernaut-XL-Ragnarok`, SDXL, the default): photo-real people, portraits and scenes, explicit-capable. Chroma1-HD: the finest natural detail (skin, hair, fabric), explicit-capable.
- **Qwen-Image 2.1 Turbo** (`Comfy-Org/Qwen-Image-2.1`, int8, with an uncensored Heretic text reader): makes AND edits
  photos with the same loaded model, so a picture and its edit never reload. Opt-in: the defaults do not change until
  its timings (Queue screen, Recent) show it is at least as good. The **explicit add-on** is a LoRA switched on per
  request (Make: Explicit add-on Auto / On / Off; chat: the brain's `explicit` flag), loaded as an extra node on top of
  the already loaded model, so ordinary pictures use the clean base. Its file and strength are live in `local.json`
  (`qwen21_explicit_lora`, `qwen21_explicit_repo`, `qwen21_explicit_strength`); a missing file says so in plain words.
- Photo editors kept as the fallback until 2.1 is proven: Qwen-Image-Edit 2511 Lightning and Phr00t's Rapid-AIO.
- Video: Wan 2.2 14B image-to-video with its 4-step LoRA and the NSFW-22 motion add-on. Upscaling: Real-ESRGAN.
- FLUX.2 klein and BigASP are removed; nothing downloads a removed model. The old helper path (diffusers) only runs a
  model that is already on disk.

**Enhance my prompt**: when a request carries `enhance`, the brain rewrites the words (Qwen's own published prompt
instructions for its picture models, fetched into the model cache at runtime, plus rules that keep every detail and
every explicit word and add no covering) while the job waits in line. If the brain is off or paused, the original words
are drawn and the phone is told.

**Real timings**: the last 100 finished jobs are kept in memory only (no prompt text): model, explicit on/off, size,
steps, seconds loading vs drawing, and whether ComfyUI had to load a model. The Queue screen's Recent section and the
brain's look-up tool read them.

**Fast switches**: ComfyUI starts with `--cache-ram 3 6` (live in `local.json`: `comfy_cache_active_gb`,
`comfy_cache_inactive_gb`) so Chroma and Qwen 2.1 both stay cached in RAM between jobs instead of being dropped at
every switch (see the comment in `Comfy.ensure`).

## Versions

| Component | Version |
|---|---|
| llama.cpp | `b12443` (commit 301db6337), `llama-b12443-bin-win-cuda-13.5-x64.zip` + `cudart-llama-bin-win-cuda-13.5-x64.zip` from the GitHub release, unzipped into `C:\FixerDesktop\llama\` (CUDA 13.5 build: supports the RTX 50 series) |
| Python packages | `requirements.txt` |

## Running

The tray icon (`tray.py`, started from the Startup folder) is the on/off switch. Only one tray can run. The tray owns
the server: **Turn off** and **Quit** ask the server to stop itself (`POST /local/quit`: every job stopped, the chat
model, the helper and ComfyUI ended, `comfy-private` wiped), force it only if it will not, then sweep anything of this
install still running. If the tray or the server dies in any way, their children die with them (job objects).

Control the install with `fixerctl.py`, never with hand kills (hand kills are how duplicate trays got made):

```
python fixerctl.py status            trays, server, port, anything of this install still running (read-only)
python fixerctl.py stop              stop everything and end every tray
python fixerctl.py start             start exactly one tray, detached from the calling session
python fixerctl.py restart
python fixerctl.py deploy [--force]  from the repo's desktop folder: copy changed files into the install, restart
```

**When the PC gives its memory back.** The phone says when the app goes to the background (`leave`) and sends the
owner's choice from Settings (`free_after_min`: 1, 3, 5, 10, 20, 60 minutes or 0 = never; default 5). Once nothing is
running or waiting and that time has passed, the engine, the chat model and ComfyUI are all stopped. The older
20-minute idle rule is the backstop and stretches to the same choice (never = never). `leave_free_minutes` in
`local.json` is the default until a phone has sent a choice. From the phone, "Free up my computer" does it at once.

**Why was that picture slow?** `GET http://127.0.0.1:8765/local/timings` (this PC only) lists the recent jobs: seconds
loading, drawing and improving the words, and whether a model had to load. No words or pictures are in it.

**Web pages on this PC cannot reach any of it.** A hostile page in a browser on this PC can aim requests at
`127.0.0.1`, and by re-pointing its own hostname there (DNS rebinding) make them look local. Three guards, each checked
by the server itself and each failing closed:
- The this-PC pages (`/local/state`, `/local/timings`, `/local/unload`, `/local/quit`) answer only when the request is
  addressed to `127.0.0.1`, `localhost` or `[::1]` on the server's own port, carries no `Origin` header, and (quit)
  carries `X-Fixer-Local: 1`. Anything else gets 404.
- ComfyUI is started through `comfy_guard.py`, which refuses (403) every request, WebSocket included, that is not
  addressed to its own loopback name and port. After each start the server sends a forged request; if it is answered,
  ComfyUI is stopped. `/local/state` shows `comfy_guarded`. Off switch: `comfy_guard: 0` in `local.json`.
- llama-server gets a fresh random key on every start (never on its command line); the server's own calls carry it.
  After each start the server checks that a call without the key is refused; if not, the chat model is stopped.
  `/local/state` shows `llama_keyed`. Off switch: `llama_key: 0` in `local.json`.

**Video length.** `seconds` up to `video_max_seconds` (`local.json`, default 30). Wan 2.2 is trained on 5-second clips,
so a longer one is made in 5-second parts, each starting from the previous part's last frame; a 30-second clip takes
about six times as long as a 5-second one, and picture quality can drift from part to part.

By hand:

```
cd C:\FixerDesktop
run.cmd                                          start the server
venv\Scripts\python pair.py "the owner's iPhone"       pair a phone (scan the QR code with the camera)
venv\Scripts\python client_test.py status        check it from this computer
venv\Scripts\python trace_test.py                prove nothing about a request is left on disk
```

## ComfyUI engine (trial, 2026-11-16)

New pictures with "Z-Image Turbo NSFW" run through a headless ComfyUI that Fixer Desktop starts and kills with
itself (127.0.0.1:8188 only, no window, no browser). Install (pinned):

```
cd C:\FixerDesktop
git clone --depth 1 --branch v0.45.5 https://github.com/comfyanonymous/ComfyUI.git comfyui
cd comfyui
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe torch==2.11.0 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

No ComfyUI-Manager, no custom nodes beyond the bundled SaveImageWebsocket. Its temp/input/output/user folders live
in `C:\FixerDesktop\comfy-private`, wiped at every start and by "clear everything"; pictures come back over its
websocket and are never saved. Model files are read from Fixer's capped folder (`Comfy-Org/Z-Image-Turbo` text
encoder fp8 + VAE, `tewea/z_image_turbo_bf16_nsfw` v2). `FIXER_COMFY=0` turns it off (everything on diffusers).
