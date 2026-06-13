# YAIL (Yet Another Image Loader) Image Server

## About

This is the image server that streams images to the YAIL client running on a
FujiNet-equipped Atari 8-bit computer. The server does the heavy lifting:
finding or generating the image, converting it to the requested native
graphics format (Graphics 8, Graphics 9, or VBXE), and streaming it over TCP.

The wire protocol is documented in [PROTOCOL.md](PROTOCOL.md).

## Features

- **Image search**: `search` finds images via the DDGS metasearch package
- **AI image generation**: any image-output model on OpenRouter
  (Gemini image models, gpt-image-1, FLUX, Recraft, ...) with one API key
- **Local image streaming**: serve a directory of images with `--paths`
- **Direct URLs**: `showurl` streams a specific image URL
- **Webcam streaming**: optional, via pygame (`[camera]` extra)
- **Multiple graphics modes**: Graphics 8 (320×220 dithered), Graphics 9
  (80×220, 16 luminances), and VBXE (320×240, 256 colors)

## Installation

Requires Python 3.10+.

```bash
python3 -m venv venv
source venv/bin/activate
pip install ".[gen]"          # core + OpenAI/Gemini generation
# optional extras: [camera] for webcam support, [netinfo] for interface listing
```

## Running

```bash
# Stream images from a local directory
yail-server --paths /path/to/images --loglevel INFO

# Choose a port (default 5556)
yail-server --paths test_images --port 5556

# Image generation via OpenRouter (one key, any model)
yail-server --openrouter-api-key sk-or-... --gen-model google/gemini-2.5-flash-image
```

`python -m yail` works as an alternative to the `yail-server` script.

## Server commands

Commands the server accepts from clients (see PROTOCOL.md for details):

- `search "<terms>"` — search for images and stream a random result
- `gen <model> "<prompt>"` — generate an image with the given model
- `gen-gemini "<prompt>"` — generate with the default Gemini model
- `showurl <url>` — stream a specific image URL
- `files` — stream a random image from `--paths`
- `video` — stream a webcam frame
- `next` — repeat the previous operation (new result/frame/regeneration)
- `gfx <mode>` — set the graphics mode (2 = Graphics 8, 4 = Graphics 9, 16 = VBXE)
- `openai-config [param] [value]` — get/set generation settings
- `quit` — end the session

## Configuration

Settings precedence (lowest to highest): process environment, env file,
command-line arguments.

Copy `deployment/env.example` to `.env` in the working directory (or pass
`--env-file path`):

```bash
OPENROUTER_API_KEY=your_openrouter_api_key_here
GEN_MODEL=google/gemini-2.5-flash-image   # any OpenRouter image-output model
```

### API key

All generation goes through [OpenRouter](https://openrouter.ai/keys) — one
key covers every image model. Model names use the OpenRouter `vendor/model`
form; legacy bare names from deployed clients (`gpt-image-1`, `gemini`,
`dall-e-*`) are resolved to OpenRouter equivalents automatically.

## Admin web UI

The server runs a lightweight admin dashboard alongside the YAIL protocol
socket (no extra dependencies). It shows server status, connected clients,
recently served images with conversion timing, and a live log tail, and
lets you view/update generation settings and API keys at runtime —
optionally persisting them to the env file.

```bash
yail-server --paths images          # UI at http://127.0.0.1:5557/
yail-server --ui-port 8080          # different UI port
yail-server --no-ui                 # disable the UI
```

By default the UI binds to `127.0.0.1` only, since it can read masked and
set unmasked API keys. To reach a remote server's UI, prefer an SSH tunnel
(`ssh -L 5557:127.0.0.1:5557 host`) over exposing it with `--ui-host`.

JSON endpoints behind the dashboard: `/api/status`, `/api/config`
(GET/POST), `/api/logs`, `/api/stats`.

### Local file serving

The `files` command is **disabled by default**: the server never serves
local images unless explicitly configured. Enable it one of three ways:

- pass `--paths /folder` on the command line (explicit opt-in), or
- set `FILES_PATH=/absolute/folder` and `FILES_ENABLED=true` in the env
  file, or
- set the folder path and tick "enable local file serving" in the web UI
  (persisting writes the two env vars above, and the new folder is
  re-indexed live without a restart).

### Streaming and search servers

The web UI also manages (all persisted to the env file, applied live):

- **Streaming** — enable/disable the client slideshow (`next` command),
  plus max retries per request, wait between retries, and the remote
  image download timeout (`STREAM_ENABLED`, `STREAM_MAX_RETRIES`,
  `STREAM_RETRY_WAIT`, `STREAM_DOWNLOAD_TIMEOUT`).
- **Image search servers** — an ordered, editable list of DDGS engines
  used by the `search` command (add/remove in the UI; `auto` means all
  engines), plus the max result count (`SEARCH_BACKENDS`,
  `SEARCH_MAX_RESULTS`). Useful when one engine starts returning no
  results — switch to another without touching the code.

## Deployment

The `deployment` directory deploys the server as a systemd service on Linux:

- `fujinet-yail.service` — systemd unit running the venv's `yail-server`
- `deploy.sh` — installs to `/opt/fujinet-yail-server`, creates the venv,
  installs the package, sets up `.env`, and enables the service
- `env.example` — example environment configuration
- `test_service.sh`, `test_gen_command.py`, `test_image_gen.py` — test scripts

```bash
cd deployment
sudo ./deploy.sh
sudo systemctl status fujinet-yail
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test suite includes golden-bytes regression tests (`tests/golden/`)
that pin the wire format to the exact output of the pre-2.0 server, plus
command-handling tests that exercise a live `ClientSession` over a
socketpair. If you touch `yail/imaging.py` or `yail/protocol.py`, the
golden tests are the contract with client binaries already in the wild.
