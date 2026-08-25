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
and Windows plugins are separate .NET class libraries (two codebases), and the one-click
"install from Settings" marketplace only works for plugins that have gone through a PR review
and signed release from the TypeWhisper maintainers. This bridge works today, identically on
both platforms, with no approval needed — you're just changing one setting in an app you already
have installed.

Everything else about TypeWhisper (the push-to-talk hotkey, pasting into whatever app is
focused, etc.) is unchanged — you're only swapping in your own Whisper as the backend.

## Requirements

- Python 3.9+ on the machine running TypeWhisper (already installed on most Macs; on Windows,
  get it from [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.12`).
- Your Home Assistant Whisper add-on running and reachable on your local network.
- Optional: [ffmpeg](https://ffmpeg.org/) — only needed if TypeWhisper ends up sending
  compressed audio the bridge can't read directly (see [Audio formats](#audio-formats) below).
  `brew install ffmpeg` (Mac) or `winget install ffmpeg` / `choco install ffmpeg` (Windows).

## 1. Find your Wyoming Whisper server's host and port

In Home Assistant: **Settings → Add-ons → Whisper → Info/Configuration**. The default port for
the add-on is **10300**. The host is your Home Assistant box's address on your network — usually
either `homeassistant.local` (if mDNS resolution works from your Mac/Windows machine) or its LAN
IP address (check your router's client list, or **Settings → System → Network** in Home
Assistant).

Wyoming has no authentication built in, so only point this at a server on a network you trust —
which your home LAN already is here.

## 2. Run the bridge

From this folder:

```bash
python3 wyoming_bridge.py --wyoming-host homeassistant.local
```

(On Windows, use `python` instead of `python3` if that's how it's set up on your PATH.) Replace
`homeassistant.local` with your Pi's IP if mDNS doesn't resolve for you. Leave this running in a
terminal — it logs each transcription as it happens. On startup it also tries a quick connection
test to your Wyoming server and tells you right away if it can't reach it.

Before wiring up TypeWhisper, you can sanity-check the connection on its own:

```bash
python3 wyoming_bridge.py --wyoming-host homeassistant.local --selftest
```

This sends one second of silence straight to your Whisper add-on and prints whatever comes back
— confirming the network path and protocol work before TypeWhisper is involved at all.

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--wyoming-host` | *(required)* | Your Home Assistant box's hostname or IP |
| `--wyoming-port` | `10300` | The Whisper add-on's Wyoming port |
| `--listen-port` | `8765` | Local port the bridge serves on |
| `--language` | *(none)* | Force a language code (e.g. `en`) instead of using TypeWhisper's own language setting |
| `--timeout` | `60` | Seconds to wait for a transcript before giving up |
| `--verbose` | off | Debug-level logging |

## 3. Point TypeWhisper at it

In TypeWhisper's transcription engine settings, choose **OpenAI Compatible** and set:

- **Base URL**: `http://127.0.0.1:8765/v1`
- **API Key**: any placeholder text, e.g. `not-needed` (the bridge ignores it — it's only there
  because the field usually can't be left empty)
- **Transcription Model**: any placeholder text, e.g. `whisper-1` (also ignored — your Home
  Assistant add-on already has its own model configured)
- **Transcription Transport**: `Batch` (or `Auto`, which resolves to the same thing here) — the
  bridge doesn't implement the realtime/WebSocket transport, which is only relevant to a couple
  of specific Azure deployments

Your existing push-to-talk hotkey (Settings → Hotkey) keeps working exactly as before — it just
transcribes through your own Whisper now.

## Audio formats

TypeWhisper normally uploads compressed audio to OpenAI-compatible endpoints and only falls back
to plain WAV if the server rejects the format with `HTTP 415`. This bridge always speaks WAV
directly and returns `415` for anything else, which — on the Mac app at least — makes it retry
with WAV automatically; the very first dictation might take one extra round trip while that
happens. If ffmpeg is installed, the bridge decodes non-WAV uploads itself instead, skipping that
extra round trip and covering any client that doesn't have the WAV-retry behavior.

## Troubleshooting

- **"Could not reach the Wyoming server" at startup**: double-check the host/port and that the
  Whisper add-on is actually started in Home Assistant. The bridge still starts up anyway, in
  case the add-on comes up later.
- **`HTTP 502` in TypeWhisper / "Wyoming server error"**: the bridge couldn't reach or got a bad
  response from your Wyoming server mid-request — same checks as above, plus firewall rules
  between your machine and the Home Assistant box.
- **`HTTP 415`**: the uploaded audio wasn't plain WAV and ffmpeg isn't installed to convert it.
  Install ffmpeg, or confirm your TypeWhisper build actually retries with WAV on a 415.
- **Nothing happens / times out**: run with `--verbose` and watch the log while you dictate;
  also try `--selftest` to isolate whether the problem is TypeWhisper-side or Wyoming-side.

## Files

- `wyoming_bridge.py` — the bridge itself; no third-party dependencies, just the Python standard
  library (plus an optional shell-out to `ffmpeg` if it's installed).
