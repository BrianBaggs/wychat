# wychat — TypeWhisper ↔ Wyoming bridge

Uses your own [Home Assistant Whisper add-on](https://www.home-assistant.io/integrations/whisper/)
(speaking the [Wyoming protocol](https://github.com/rhasspy/wyoming)) as the speech-to-text
backend for [TypeWhisper](https://www.typewhisper.com/en/)'s push-to-talk dictation, on both
macOS and Windows.

## How it works

TypeWhisper's Mac and Windows apps both ship a built-in **"OpenAI Compatible"** transcription
engine, meant for pointing at local servers like Ollama or LM Studio — no plugin install
required. `wyoming_bridge.py` is a small local server that speaks that same OpenAI-compatible
API on one side, and the real Wyoming protocol on the other. It forwards each dictation to your
Home Assistant box's Whisper add-on and hands the transcript back.

This was chosen over a native TypeWhisper plugin deliberately: Mac plugins are Swift `.bundle`s
and Windows plugins are separate .NET class libraries — two codebases — and the one-click
"install from Settings" marketplace only works for plugins that have gone through a PR review
and signed release from the TypeWhisper maintainers. This bridge works today, identically on
both platforms, with no approval needed.

Everything else about TypeWhisper (the push-to-talk hotkey, pasting into whatever app is
focused, etc.) is unchanged — you're only swapping in your own Whisper as the backend.

## Requirements

- **Python 3.10+** — the bridge itself has **zero required dependencies**, just the standard
  library. Already on most Macs; on Windows get it from
  [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.12`.
- Your Home Assistant Whisper add-on, running and reachable on your local network.
- *Optional:* [ffmpeg](https://ffmpeg.org/) — only needed if TypeWhisper sends compressed audio
  the bridge can't read directly (see [Audio formats](#audio-formats)). The `--setup` wizard
  offers to install it for you via your system's own package manager (`brew` on macOS, `winget`
  on Windows) — never a bundled or downloaded binary.

## Install

```bash
git clone <this-repo-url> wychat
cd wychat
pip install .
```
OR instead of pip you can replace it with pipx if you need to.

This installs the bridge and a `wyoming-bridge` command on your `PATH`. (A virtual environment
is recommended, as always: `python3 -m venv .venv && source .venv/bin/activate` first, or the
equivalent `.venv\Scripts\activate` on Windows.)

Contributing or running the tests instead? Install the dev tools too:

```bash
pip install -e ".[dev]"
```

`-e` (editable) means changes to `wyoming_bridge.py` take effect immediately, without
reinstalling. `pyproject.toml` is the single source of truth for what's required — there's no
separate `requirements.txt` to keep in sync.

## Quick start

```bash
wyoming-bridge --setup
```

This interactively asks for your Wyoming server's host/IP and port, checks that it's reachable,
optionally offers to install ffmpeg, saves your settings to `~/.wyoming_bridge/config.json`, and
optionally installs a background service so the bridge starts automatically at login. At the
end it prints (and copies to your clipboard) the URL to paste into TypeWhisper.

Didn't install via pip? Run `python3 wyoming_bridge.py --setup` from this folder instead —
everywhere below, `wyoming-bridge` and `python3 wyoming_bridge.py` are interchangeable.

## Manual setup

If you'd rather not use the wizard, or want to script it:

### 1. Find your Wyoming Whisper server's host and port

In Home Assistant: **Settings → Add-ons → Whisper → Info/Configuration**. The default port for
the add-on is **10300**. The host is your Home Assistant box's address on your network — usually
either `homeassistant.local` (if mDNS resolution works from your Mac/Windows machine) or its LAN
IP address (check your router's client list, or **Settings → System → Network** in Home
Assistant).

Wyoming has no authentication built in, so only point this at a server on a network you trust —
which your home LAN already is here.

### 2. Sanity-check the connection (optional but recommended)

```bash
wyoming-bridge --wyoming-host homeassistant.local --selftest
```

This sends one second of silence straight to your Whisper add-on and prints whatever comes back
— confirming the network path and protocol work before TypeWhisper is involved at all. An empty
transcript is the *correct* result for silence; the point is that the round trip worked.

### 3. Run it

```bash
wyoming-bridge --wyoming-host homeassistant.local
```

Leave this running in a terminal — it logs each transcription as it happens, and tries a quick
connection test to your Wyoming server on startup. To keep it running automatically instead
(across reboots and logouts), see [Background service](#background-service) below.

## Point TypeWhisper at it

In TypeWhisper's transcription engine settings, choose **OpenAI Compatible** and set:

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8765/v1` |
| API Key | any placeholder text, e.g. `not-needed` (ignored — the field just can't be empty) |
| Transcription Model | any placeholder text, e.g. `whisper-1` (ignored — your add-on already has a model configured) |
| Transcription Transport | `Batch` or `Auto` (the bridge doesn't implement the realtime/WebSocket transport, which is only relevant to a couple of specific Azure deployments) |

Your existing push-to-talk hotkey (Settings → Hotkey) keeps working exactly as before — it just
transcribes through your own Whisper now.

## Background service

Instead of a terminal window, `wyoming-bridge` can install itself as a proper background
service — `launchd` on macOS, Task Scheduler on Windows — that starts at login and restarts if
it crashes:

```bash
wyoming-bridge --wyoming-host homeassistant.local --install-service
```

This also saves your settings and prints/copies the TypeWhisper base URL, same as `--setup`'s
final step (which offers this same install interactively). To remove it later:

```bash
wyoming-bridge --uninstall-service
```

Logs from the installed service go to `~/Library/Logs/wyoming-bridge.log` on macOS. On Windows,
`schtasks /Run /TN WyomingBridge` starts it immediately without waiting for the next logon.

## All options

| Flag | Default | Purpose |
|---|---|---|
| `--wyoming-host` | *(required, except with `--setup`/`--uninstall-service`)* | Your Home Assistant box's hostname or IP |
| `--wyoming-port` | `10300` | The Whisper add-on's Wyoming port |
| `--listen-host` | `127.0.0.1` | Local address the bridge listens on |
| `--listen-port` | `8765` | Local port the bridge listens on |
| `--language` | *(none)* | Force a language code (e.g. `en`) instead of using TypeWhisper's own language setting |
| `--timeout` | `60` | Seconds to wait for a transcript before giving up |
| `--setup` | | Interactive wizard: prompts for the above, saves them, offers to install ffmpeg and the background service |
| `--selftest` | | Send silence straight to the Wyoming server and print the result, without starting the HTTP server |
| `--install-service` | | Install the background service non-interactively (needs `--wyoming-host` etc. passed as flags) |
| `--uninstall-service` | | Remove a previously installed background service |
| `--verbose` | | Debug-level logging |

Settings are saved to `~/.wyoming_bridge/config.json` (including the exact TypeWhisper base URL)
every time the bridge starts or `--setup`/`--install-service` runs, so you always have a record
of what's currently configured.

## Audio formats

TypeWhisper normally uploads compressed audio to OpenAI-compatible endpoints and only falls back
to plain WAV if the server rejects the format with `HTTP 415`. This bridge always speaks WAV
directly and returns `415` for anything else, which — on the Mac app at least — makes it retry
with WAV automatically; the very first dictation might take one extra round trip while that
happens. If ffmpeg is installed, the bridge decodes non-WAV uploads itself instead, skipping that
extra round trip and covering any client that doesn't have the WAV-retry behavior. `--setup`
offers to install ffmpeg via your system's own package manager (`brew`/`winget`) if it's missing.

## Troubleshooting

- **"Could not reach the Wyoming server" at startup**: double-check the host/port and that the
  Whisper add-on is actually started in Home Assistant. The bridge still starts up anyway, in
  case the add-on comes up later.
- **`Connection refused`**: the host is reachable but nothing is listening on that port — the
  add-on likely isn't running, or is on a different port than you think.
- **`Operation timed out`**: the host isn't responding at all. Confirm the IP is actually your
  Home Assistant box (try opening `http://<host>:8123` in a browser) and that nothing in between
  is blocking the connection. If you just changed the add-on's port, give it a few seconds to
  restart before retrying.
- **`HTTP 502` in TypeWhisper / "Wyoming server error"**: the bridge couldn't reach or got a bad
  response from your Wyoming server mid-request — same checks as above, plus firewall rules
  between your machine and the Home Assistant box.
- **`HTTP 415`**: the uploaded audio wasn't plain WAV and ffmpeg isn't installed to convert it.
  Install ffmpeg, or confirm your TypeWhisper build actually retries with WAV on a 415.
- **Nothing happens / times out**: run with `--verbose` and watch the log while you dictate;
  also try `--selftest` to isolate whether the problem is TypeWhisper-side or Wyoming-side.

## Development

```bash
pip install -e ".[dev]"
```

Run the tests (real sockets and a real HTTP server are used wherever practical — no mocking
network I/O just for the sake of it):

```bash
pytest
```

With coverage (the project targets 100%; CI enforces a 95% floor):

```bash
coverage run -m pytest && coverage report -m
```

Lint and type-check:

```bash
ruff check .
pyright
```

All of the above run automatically on every push to `main` and on every pull request — see
`.github/workflows/ci.yml`.

## Files

| File | Purpose |
|---|---|
| `wyoming_bridge.py` | The bridge itself — stdlib only, no dependencies |
| `test_wyoming_bridge.py` | Test suite (pytest) |
| `pyproject.toml` | Package metadata, dev dependencies, and tool config (pytest/coverage/ruff/pyright) |
| `.github/workflows/ci.yml` | CI: tests + linters on pushes to `main` and pull requests |
