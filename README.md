# Fixer

A private AI assistant for iPhone that runs entirely on the phone. Open a photo and say what you want in plain words
("take the people in the background out", "make it golden hour", "cut me out"); Fixer looks at the photo, works out
what you mean, and does it. The Ask tab is an ordinary chat assistant (writing, questions, reading photos and
screenshots). Nothing leaves the phone unless you turn on "Use my PC" (below). You set the rules in Settings,
including which Hugging Face models it uses.

**Use my PC (optional).** [`desktop/`](desktop/) is Fixer Desktop: a small server for a Windows PC with an NVIDIA
card. Paired by QR code, it runs chat on a bigger vision model and makes/edits images with FLUX.2, end-to-end
encrypted with a per-phone key, keeping nothing on the PC.

Shipped through TestFlight: every push to `main` builds on a GitHub-hosted Mac and uploads (`.github/workflows/testflight.yml`).

## Models (downloaded on first use)

| Job | Default | License |
|---|---|---|
| Brain (vision + chat) | `EZCon/Huihui-Qwen3-VL-4B-Instruct-abliterated-4bit-mlx` via [mlx-swift-lm](https://github.com/ml-explore/mlx-swift-lm); any MLX VLM can be pasted in | Apache-2.0 (Qwen3-VL) |
| Remove | [LaMa Core ML](https://huggingface.co/mlboydaisuke/LaMa-CoreML) | Apache-2.0 |
| Upscale | [Real-ESRGAN x4 Core ML](https://huggingface.co/mlboydaisuke/Real-ESRGAN-x4-CoreML) | BSD-3-Clause |
| Redraw / make | `apple/coreml-stable-diffusion-v1-5-palettized` via [ml-stable-diffusion](https://github.com/apple/ml-stable-diffusion); any Core ML SD 1.x/2.x repo can be pasted in | CreativeML OpenRAIL-M |
| Cut out | Apple Vision (built in) | — |
| Output filter | [Bikini Bottom](https://github.com/TopScrech/Bikini-Bottom) classifier, bundled; strictness set in Rules | MIT |

## Setup (once)

Repo variables `TEAM_ID`, `BUNDLE_ID`; secrets `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8` (an App Store Connect
API key with Admin access); an App Store Connect app record for the bundle ID.
