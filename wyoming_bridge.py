#!/usr/bin/env python3
"""Bridges TypeWhisper's "OpenAI Compatible" transcription engine to a Wyoming ASR server.

Run this next to (or on the same machine as) TypeWhisper, pointed at the Wyoming
Whisper add-on on your Home Assistant box. Either run it directly:

    python3 wyoming_bridge.py --setup

or, after `pip install .`, via the installed command:

    wyoming-bridge --setup

`--setup` walks through the settings interactively and can install a background
service (launchd on macOS, Task Scheduler on Windows) so it starts automatically.
Then in TypeWhisper, set the transcription engine to "OpenAI Compatible" with a
base URL of http://127.0.0.1:8765/v1 (any API key / model value works, they are
ignored). See README.md for the full walkthrough.

Wire format reference: https://github.com/rhasspy/wyoming
"""

import argparse
import asyncio
import contextlib
import json
import logging
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import wave
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

LOG = logging.getLogger("wyoming_bridge")

WYOMING_SAMPLE_RATE = 16000
WYOMING_SAMPLE_WIDTH = 2  # bytes (16-bit PCM)
WYOMING_CHANNELS = 1
AUDIO_CHUNK_BYTES = 8192


class UnsupportedAudioError(Exception):
    """Raised when the uploaded audio can't be turned into 16kHz mono PCM."""


# ---------------------------------------------------------------------------
# Wyoming protocol client
#
# Each event on the wire is: a JSON header line, then (if data_length > 0) that
# many bytes of a separate JSON object, then (if payload_length > 0) that many
# raw bytes. The header line never embeds "data" inline -- confirmed against
# rhasspy/wyoming's event.py, not just the informal docs.
# ---------------------------------------------------------------------------

async def _write_event(
    writer: asyncio.StreamWriter,
    event_type: str,
    data: dict | None = None,
    payload: bytes | None = None,
) -> None:
    header: dict[str, object] = {"type": event_type}

    data_bytes = None
    if data:
        data_bytes = json.dumps(data).encode("utf-8")
        header["data_length"] = len(data_bytes)

    if payload:
        header["payload_length"] = len(payload)

    writer.write(json.dumps(header).encode("utf-8") + b"\n")
    if data_bytes:
        writer.write(data_bytes)
    if payload:
        writer.write(payload)
    await writer.drain()


async def _read_event(reader: asyncio.StreamReader):
    line = await reader.readline()
    if not line:
        return None
    header = json.loads(line)

    data = {}
    data_length = header.get("data_length") or 0
    if data_length > 0:
        data = json.loads(await reader.readexactly(data_length))

    payload = None
    payload_length = header.get("payload_length") or 0
    if payload_length > 0:
        payload = await reader.readexactly(payload_length)

    return header["type"], data, payload


async def wyoming_transcribe(
    host: str,
    port: int,
    pcm_audio: bytes,
    language: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Sends 16kHz/mono/16-bit PCM audio to a Wyoming ASR server, returns the transcript text.

    Opens a fresh connection per call -- the reference server closes the
    connection after replying to audio-stop, so connections aren't reused.
    """
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    try:
        if language:
            await _write_event(writer, "transcribe", data={"language": language})

        audio_format = {
            "rate": WYOMING_SAMPLE_RATE,
            "width": WYOMING_SAMPLE_WIDTH,
            "channels": WYOMING_CHANNELS,
        }
        await _write_event(writer, "audio-start", data=audio_format)

        for offset in range(0, len(pcm_audio), AUDIO_CHUNK_BYTES):
            chunk = pcm_audio[offset:offset + AUDIO_CHUNK_BYTES]
            await _write_event(writer, "audio-chunk", data=audio_format, payload=chunk)

        await _write_event(writer, "audio-stop")

        while True:
            event = await asyncio.wait_for(_read_event(reader), timeout=timeout)
            if event is None:
                raise ConnectionError("Wyoming server closed the connection before sending a transcript.")
            event_type, data, _payload = event
            if event_type == "transcript":
                return data.get("text", "")
            # Ignore anything else (e.g. an info/describe reply) and keep waiting.
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def wyoming_describe(host: str, port: int, timeout: float = 5.0) -> str:
    """Opens a connection, sends 'describe', and returns the type of whatever comes back."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    try:
        await _write_event(writer, "describe")
        event = await asyncio.wait_for(_read_event(reader), timeout=timeout)
        if event is None:
            raise ConnectionError("Server closed the connection without responding.")
        return event[0]
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# ---------------------------------------------------------------------------
# Audio handling: TypeWhisper's "OpenAI Compatible" engine uploads compressed
# M4A by default and falls back to WAV if the server returns 415, so most
# requests here will actually be WAV. ffmpeg (if present) covers everything else.
# ---------------------------------------------------------------------------

def pcm_from_upload(file_bytes: bytes, filename: str | None, content_type: str | None) -> bytes:
    looks_like_wav = (
        file_bytes[:4] == b"RIFF"
        or (content_type or "").lower() in ("audio/wav", "audio/x-wav", "audio/wave")
        or (filename or "").lower().endswith(".wav")
    )
    if looks_like_wav:
        return _extract_wav_pcm(file_bytes)
    return _transcode_with_ffmpeg(file_bytes)


def _extract_wav_pcm(wav_bytes: bytes) -> bytes:
    with wave.open(BytesIO(wav_bytes), "rb") as wav_file:
        rate = wav_file.getframerate()
        width = wav_file.getsampwidth()
        channels = wav_file.getnchannels()
        frames = wav_file.readframes(wav_file.getnframes())

    if (rate, width, channels) == (WYOMING_SAMPLE_RATE, WYOMING_SAMPLE_WIDTH, WYOMING_CHANNELS):
        return frames

    LOG.debug(
        "Uploaded WAV is %dHz/%d-byte/%dch, not the expected 16kHz/16-bit/mono -- transcoding.",
        rate, width, channels,
    )
    return _transcode_with_ffmpeg(wav_bytes)


def _transcode_with_ffmpeg(audio_bytes: bytes) -> bytes:
    if shutil.which("ffmpeg") is None:
        raise UnsupportedAudioError(
            "This upload isn't plain 16kHz/mono WAV and ffmpeg isn't installed to convert it. "
            "Install ffmpeg (e.g. 'brew install ffmpeg' or 'winget install ffmpeg'), or the "
            "client may automatically retry with a WAV upload instead."
        )

    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "-",
            "-f", "s16le", "-ar", str(WYOMING_SAMPLE_RATE), "-ac", str(WYOMING_CHANNELS),
            "-",
        ],
        input=audio_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode(errors="replace").strip()
        raise UnsupportedAudioError(f"ffmpeg could not decode the uploaded audio: {stderr}")
    return result.stdout


# ---------------------------------------------------------------------------
# Minimal multipart/form-data parsing (cgi.FieldStorage is gone in Python 3.13+)
# ---------------------------------------------------------------------------

@dataclass
class MultipartField:
    content: bytes
    filename: str | None
    content_type: str | None


def parse_multipart(body: bytes, boundary: bytes) -> dict[str, MultipartField]:
    delimiter = b"--" + boundary
    chunks = body.split(delimiter)
    fields: dict[str, MultipartField] = {}

    # chunks[0] is the preamble before the first boundary; the last chunk is the
    # closing "--\r\n" marker. Both are skipped by only iterating the middle ones.
    for chunk in chunks[1:-1]:
        if not (chunk.startswith(b"\r\n") and chunk.endswith(b"\r\n")):
            continue
        chunk = chunk[2:-2]  # drop exactly the framing CRLFs, never touching content bytes

        if b"\r\n\r\n" not in chunk:
            continue
        header_blob, content = chunk.split(b"\r\n\r\n", 1)

        headers = {}
        for line in header_blob.split(b"\r\n"):
            if b":" not in line:
                continue
            key, _, value = line.partition(b":")
            headers[key.strip().lower().decode("ascii", "ignore")] = value.strip().decode("utf-8", "ignore")

        name, filename = _parse_content_disposition(headers.get("content-disposition", ""))
        if name:
            fields[name] = MultipartField(content=content, filename=filename, content_type=headers.get("content-type"))

    return fields


def _parse_content_disposition(value: str):
    name = None
    filename = None
    for piece in value.split(";"):
        piece = piece.strip()
        if piece.startswith("name="):
            name = piece[len("name="):].strip('"')
        elif piece.startswith("filename="):
            filename = piece[len("filename="):].strip('"')
    return name, filename


# ---------------------------------------------------------------------------
# HTTP server exposing the OpenAI-compatible surface TypeWhisper expects
# ---------------------------------------------------------------------------

class BridgeHandler(BaseHTTPRequestHandler):
    wyoming_host = ""
    wyoming_port = 10300
    forced_language: str | None = None
    request_timeout = 60.0

    server_version = "WyomingBridge/1.0"

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path.split("?", 1)[0].rstrip("/").endswith("/v1/models"):
            self._send_json(200, {"object": "list", "data": [{"id": "wyoming-whisper", "object": "model"}]})
            return
        self._send_json(200, {"status": "ok", "bridge": "wyoming-whisper"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.endswith("/audio/translations"):
            self._send_json(400, {
                "error": "Wyoming's ASR protocol has no translation mode -- only plain transcription is supported.",
            })
            return
        if not path.endswith("/audio/transcriptions"):
            self._send_json(404, {"error": f"Unknown endpoint: {self.path}"})
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type or "boundary=" not in content_type:
            self._send_json(400, {"error": "Expected a multipart/form-data upload with a 'file' field."})
            return
        boundary = content_type.split("boundary=", 1)[1].strip().strip('"').encode("utf-8")

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        fields = parse_multipart(body, boundary)

        file_field = fields.get("file")
        if file_field is None:
            self._send_json(400, {"error": "No 'file' field found in the upload."})
            return

        language_field = fields.get("language")
        language = self.forced_language or (
            language_field.content.decode("utf-8", "ignore").strip() if language_field else None
        )

        try:
            pcm_audio = pcm_from_upload(file_field.content, file_field.filename, file_field.content_type)
        except UnsupportedAudioError as error:
            LOG.warning("Rejecting upload we can't decode: %s", error)
            self._send_json(415, {"error": str(error)})
            return

        try:
            text = asyncio.run(
                wyoming_transcribe(
                    self.wyoming_host, self.wyoming_port, pcm_audio,
                    language=language or None, timeout=self.request_timeout,
                )
            )
        except Exception as error:
            LOG.error("Wyoming transcription failed: %s", error)
            self._send_json(502, {"error": f"Wyoming server error: {error}"})
            return

        LOG.info("Transcribed %d bytes of audio -> %r", len(pcm_audio), text)
        self._send_json(200, {"text": text})

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def normalize_host(raw_host: str) -> str:
    """Tolerates common paste mistakes: a URL scheme, a trailing path, or an embedded port.

    --wyoming-host wants a bare hostname/IP for a raw TCP connection, but it's an easy slip to
    paste something URL-shaped instead (TypeWhisper's own base URL field does want a full URL).
    """
    host = raw_host.strip()

    for scheme in ("http://", "https://", "tcp://"):
        if host.lower().startswith(scheme):
            LOG.warning(
                "--wyoming-host had a %r prefix -- stripping it. Pass just the bare hostname or IP, e.g. 192.168.7.55.",
                scheme,
            )
            host = host[len(scheme):]
            break

    if "/" in host:
        host = host.split("/", 1)[0]

    if ":" in host:
        host_part, _, port_part = host.rpartition(":")
        if host_part and port_part.isdigit():
            LOG.warning(
                "--wyoming-host had a port attached (:%s) -- ignoring it. Pass --wyoming-port %s instead "
                "if that's really your Wyoming server's port (the Whisper add-on default is 10300).",
                port_part, port_part,
            )
            host = host_part

    return host


def check_wyoming_reachable(host: str, port: int, timeout: float = 3.0) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            LOG.info("Confirmed the Wyoming server at %s:%d is reachable.", host, port)
    except OSError as error:
        LOG.warning(
            "Could not reach the Wyoming server at %s:%d right now (%s). Starting anyway -- "
            "double check the host/port and that the Whisper add-on is running.",
            host, port, error,
        )


# ---------------------------------------------------------------------------
# Saved config + optional background service (launchd on macOS, Task Scheduler
# on Windows) so the bridge can start automatically instead of living in a
# terminal window. All filesystem/process side effects go through the small
# helpers below so tests can redirect or mock them individually.
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".wyoming_bridge"
CONFIG_PATH = CONFIG_DIR / "config.json"
SERVICE_LABEL = "com.wychat.wyoming-bridge"
WINDOWS_TASK_NAME = "WyomingBridge"


def base_url_for(listen_host: str, listen_port: int) -> str:
    return f"http://{listen_host}:{listen_port}/v1"


def save_config(args: argparse.Namespace) -> Path:
    """Persists the last-used settings and the TypeWhisper base URL to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = {
        "base_url": base_url_for(args.listen_host, args.listen_port),
        "wyoming_host": args.wyoming_host,
        "wyoming_port": args.wyoming_port,
        "listen_host": args.listen_host,
        "listen_port": args.listen_port,
        "language": args.language,
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return CONFIG_PATH


def copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard copy; returns whether it worked."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return True
        if system == "Windows":
            subprocess.run(["clip"], input=text.encode("utf-8"), check=True)
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def _service_command_args(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable, str(Path(__file__).resolve()),
        "--wyoming-host", args.wyoming_host,
        "--wyoming-port", str(args.wyoming_port),
        "--listen-host", args.listen_host,
        "--listen-port", str(args.listen_port),
        "--timeout", str(args.timeout),
    ]
    if args.language:
        command += ["--language", args.language]
    return command


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _launchd_log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "wyoming-bridge.log"


def _install_launchd(args: argparse.Namespace) -> None:
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = _launchd_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": _service_command_args(args),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    with open(plist_path, "wb") as plist_file:
        plistlib.dump(plist, plist_file)

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)
    result = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, check=False)
    if result.returncode != 0:
        LOG.error("launchctl load failed: %s", result.stderr.decode(errors="replace").strip())
        raise SystemExit(1)

    LOG.info("Installed a launchd service (%s) -- it will start at login and restart if it crashes.", SERVICE_LABEL)
    LOG.info("Logs: %s", log_path)
    LOG.info("To remove it: python3 %s --uninstall-service", Path(__file__).name)


def _uninstall_launchd() -> None:
    plist_path = _launchd_plist_path()
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)
    if plist_path.exists():
        plist_path.unlink()
        LOG.info("Removed and unloaded the launchd service.")
    else:
        LOG.info("No launchd service was installed.")


def _windows_wrapper_bat_path() -> Path:
    return CONFIG_DIR / "run_wyoming_bridge.bat"


def _install_windows_task(args: argparse.Namespace) -> None:
    bat_path = _windows_wrapper_bat_path()
    bat_path.parent.mkdir(parents=True, exist_ok=True)
    command_line = subprocess.list2cmdline(_service_command_args(args))
    bat_path.write_text(f"@echo off\r\n{command_line}\r\n", encoding="utf-8")

    result = subprocess.run(
        [
            "schtasks", "/Create", "/TN", WINDOWS_TASK_NAME,
            "/TR", str(bat_path),
            "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
        ],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        LOG.error("schtasks failed: %s", result.stderr.decode(errors="replace").strip())
        raise SystemExit(1)

    LOG.info("Installed a scheduled task (%s) -- it will start at logon.", WINDOWS_TASK_NAME)
    LOG.info("To start it immediately: schtasks /Run /TN %s", WINDOWS_TASK_NAME)
    LOG.info("To remove it: python wyoming_bridge.py --uninstall-service")


def _uninstall_windows_task() -> None:
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
        capture_output=True, check=False,
    )
    if result.returncode == 0:
        LOG.info("Removed the scheduled task.")
    else:
        LOG.info("No scheduled task was installed (or it was already removed).")
    bat_path = _windows_wrapper_bat_path()
    if bat_path.exists():
        bat_path.unlink()


def install_service(args: argparse.Namespace) -> None:
    system = platform.system()
    if system == "Darwin":
        _install_launchd(args)
    elif system == "Windows":
        _install_windows_task(args)
    else:
        LOG.error("Automatic background-service setup isn't implemented for %s.", system)
        LOG.info("Run it manually instead: %s", " ".join(_service_command_args(args)))
        raise SystemExit(1)


def uninstall_service() -> None:
    system = platform.system()
    if system == "Darwin":
        _uninstall_launchd()
    elif system == "Windows":
        _uninstall_windows_task()
    else:
        LOG.error("Automatic background-service setup isn't implemented for %s.", system)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Interactive setup wizard (`--setup`): prompts for the settings above instead
# of requiring them all as flags, and optionally offers to install ffmpeg via
# the system's own package manager (never a bundled/downloaded binary).
# ---------------------------------------------------------------------------

def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def _prompt_yes_no(question: str, default: bool = False) -> bool:
    label = "Y/n" if default else "y/N"
    answer = input(f"{question} [{label}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _detect_package_manager() -> tuple[str, ...] | None:
    system = platform.system()
    if system == "Darwin" and shutil.which("brew"):
        return ("brew", "install", "ffmpeg")
    if system == "Windows" and shutil.which("winget"):
        return ("winget", "install", "ffmpeg")
    return None


def _offer_ffmpeg_install() -> None:
    if shutil.which("ffmpeg") is not None:
        return
    print(
        "\nffmpeg isn't installed. It's optional -- only used as a fallback for non-WAV audio "
        "uploads -- but installing it avoids one extra round trip on every dictation."
    )
    command = _detect_package_manager()
    if command is None:
        print("Install it yourself when convenient: https://ffmpeg.org/download.html")
        return
    if _prompt_yes_no(f"Install it now with '{' '.join(command)}'?"):
        subprocess.run(command, check=False)


def run_setup_wizard(initial_args: argparse.Namespace) -> None:
    print("Wyoming bridge setup")
    print("=====================")

    wyoming_host = ""
    while not wyoming_host:
        wyoming_host = normalize_host(
            _prompt("Wyoming server host/IP (your Home Assistant box)", initial_args.wyoming_host or "")
        )

    wyoming_port = int(_prompt("Wyoming server port", str(initial_args.wyoming_port)))
    listen_port = int(_prompt("Local port for TypeWhisper to connect to", str(initial_args.listen_port)))
    language = _prompt("Force a language code, e.g. 'en' (blank = auto-detect)", initial_args.language or "") or None

    args = argparse.Namespace(
        wyoming_host=wyoming_host, wyoming_port=wyoming_port,
        listen_host=initial_args.listen_host, listen_port=listen_port,
        language=language, timeout=initial_args.timeout,
    )

    print(f"\nChecking {wyoming_host}:{wyoming_port} ...")
    check_wyoming_reachable(wyoming_host, wyoming_port)

    _offer_ffmpeg_install()

    config_path = save_config(args)
    print(f"\nSettings saved to {config_path}")

    base_url = base_url_for(args.listen_host, args.listen_port)
    if _prompt_yes_no("Install a background service so this starts automatically at login?", default=True):
        install_service(args)
        if copy_to_clipboard(base_url):
            print(f"Copied {base_url} to your clipboard -- paste it into TypeWhisper's base URL field.")
        else:
            print(f"TypeWhisper base URL: {base_url}")
    else:
        print(f"\nRun it with: {' '.join(_service_command_args(args))}")
        print(f"TypeWhisper base URL: {base_url}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--wyoming-host", default=None,
        help="Host/IP of the Wyoming ASR server. Not needed with --setup/--uninstall-service.",
    )
    parser.add_argument(
        "--wyoming-port", type=int, default=10300,
        help="Port of the Wyoming ASR server (default: 10300).",
    )
    parser.add_argument(
        "--listen-host", default="127.0.0.1",
        help="Local address to listen on (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--listen-port", type=int, default=8765,
        help="Local port to listen on (default: 8765).",
    )
    parser.add_argument(
        "--language", default=None,
        help="Force a language code (e.g. 'en') for every request.",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0,
        help="Seconds to wait for the Wyoming server (default: 60).",
    )
    parser.add_argument(
        "--setup", action="store_true",
        help="Interactively prompt for settings, save them, and optionally install the background service.",
    )
    parser.add_argument(
        "--selftest", action="store_true",
        help="Send one second of silence directly to the Wyoming server and exit, without starting the HTTP server.",
    )
    parser.add_argument(
        "--install-service", action="store_true",
        help=(
            "Install a background service (launchd on macOS, Task Scheduler on Windows) that "
            "runs the bridge automatically, then exit."
        ),
    )
    parser.add_argument(
        "--uninstall-service", action="store_true",
        help="Remove a previously installed background service, then exit.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.uninstall_service:
        uninstall_service()
        return

    if args.setup:
        run_setup_wizard(args)
        return

    if args.wyoming_host is None:
        parser.error("--wyoming-host is required (unless using --setup/--uninstall-service).")

    args.wyoming_host = normalize_host(args.wyoming_host)

    if args.install_service:
        check_wyoming_reachable(args.wyoming_host, args.wyoming_port)
        save_config(args)
        install_service(args)
        base_url = base_url_for(args.listen_host, args.listen_port)
        if copy_to_clipboard(base_url):
            LOG.info("Copied %s to your clipboard -- paste it into TypeWhisper's base URL field.", base_url)
        else:
            LOG.info("TypeWhisper base URL: %s", base_url)
        return

    if args.selftest:
        LOG.info("Sending 1 second of silence to %s:%d ...", args.wyoming_host, args.wyoming_port)
        silence = b"\x00" * (WYOMING_SAMPLE_RATE * WYOMING_SAMPLE_WIDTH)
        try:
            text = asyncio.run(wyoming_transcribe(
                args.wyoming_host, args.wyoming_port, silence,
                language=args.language, timeout=args.timeout,
            ))
        except Exception as error:
            LOG.error("Self-test failed: %s", error)
            raise SystemExit(1) from error
        LOG.info("Self-test succeeded -- the server responded with transcript: %r", text)
        LOG.info("(An empty string is normal for silence; the point is that the round trip worked.)")
        return

    check_wyoming_reachable(args.wyoming_host, args.wyoming_port)
    config_path = save_config(args)

    BridgeHandler.wyoming_host = args.wyoming_host
    BridgeHandler.wyoming_port = args.wyoming_port
    BridgeHandler.forced_language = args.language
    BridgeHandler.request_timeout = args.timeout

    server = ThreadingHTTPServer((args.listen_host, args.listen_port), BridgeHandler)
    base_url = base_url_for(args.listen_host, args.listen_port)
    LOG.info("Wyoming bridge listening on http://%s:%d", args.listen_host, args.listen_port)
    LOG.info("Forwarding transcription requests to Wyoming server at %s:%d", args.wyoming_host, args.wyoming_port)
    LOG.info("In TypeWhisper, set the 'OpenAI Compatible' engine's base URL to: %s", base_url)
    LOG.info("(Settings saved to %s for next time.)", config_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down.")
    finally:
        server.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
