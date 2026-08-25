#!/usr/bin/env python3
"""Bridges TypeWhisper's "OpenAI Compatible" transcription engine to a Wyoming ASR server.

Run this next to (or on the same machine as) TypeWhisper, pointed at the Wyoming
Whisper add-on on your Home Assistant box:

    python3 wyoming_bridge.py --wyoming-host 192.168.1.50

Then in TypeWhisper, set the transcription engine to "OpenAI Compatible" with a
base URL of http://127.0.0.1:8765/v1 (any API key / model value works, they are
ignored). See README.md for the full walkthrough.

Wire format reference: https://github.com/rhasspy/wyoming
"""

import argparse
import asyncio
import json
import logging
import shutil
import socket
import subprocess
import wave
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Dict, Optional

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

async def _write_event(writer: asyncio.StreamWriter, event_type: str, data: Optional[dict] = None, payload: Optional[bytes] = None) -> None:
    header: Dict[str, object] = {"type": event_type}

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


async def wyoming_transcribe(host: str, port: int, pcm_audio: bytes, language: Optional[str] = None, timeout: float = 60.0) -> str:
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
        try:
            await writer.wait_closed()
        except Exception:
            pass


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
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Audio handling: TypeWhisper's "OpenAI Compatible" engine uploads compressed
# M4A by default and falls back to WAV if the server returns 415, so most
# requests here will actually be WAV. ffmpeg (if present) covers everything else.
# ---------------------------------------------------------------------------

def pcm_from_upload(file_bytes: bytes, filename: Optional[str], content_type: Optional[str]) -> bytes:
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

    LOG.debug("Uploaded WAV is %dHz/%d-byte/%dch, not the expected 16kHz/16-bit/mono -- transcoding.", rate, width, channels)
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise UnsupportedAudioError(f"ffmpeg could not decode the uploaded audio: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


# ---------------------------------------------------------------------------
# Minimal multipart/form-data parsing (cgi.FieldStorage is gone in Python 3.13+)
# ---------------------------------------------------------------------------

@dataclass
class MultipartField:
    content: bytes
    filename: Optional[str]
    content_type: Optional[str]


def parse_multipart(body: bytes, boundary: bytes) -> Dict[str, MultipartField]:
    delimiter = b"--" + boundary
    chunks = body.split(delimiter)
    fields: Dict[str, MultipartField] = {}

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
    forced_language: Optional[str] = None
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
            self._send_json(400, {"error": "Wyoming's ASR protocol has no translation mode -- only plain transcription is supported."})
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
            LOG.warning("--wyoming-host had a %r prefix -- stripping it. Pass just the bare hostname or IP, e.g. 192.168.7.55.", scheme)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wyoming-host", required=True, help="Host/IP of the Wyoming ASR server (your Home Assistant box).")
    parser.add_argument("--wyoming-port", type=int, default=10300, help="Port of the Wyoming ASR server (default: 10300).")
    parser.add_argument("--listen-host", default="127.0.0.1", help="Local address to listen on (default: 127.0.0.1).")
    parser.add_argument("--listen-port", type=int, default=8765, help="Local port to listen on (default: 8765).")
    parser.add_argument("--language", default=None, help="Force a language code (e.g. 'en') for every request.")
    parser.add_argument("--timeout", type=float, default=60.0, help="Seconds to wait for the Wyoming server (default: 60).")
    parser.add_argument("--selftest", action="store_true", help="Send one second of silence directly to the Wyoming server and exit, without starting the HTTP server.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.wyoming_host = normalize_host(args.wyoming_host)

    if args.selftest:
        LOG.info("Sending 1 second of silence to %s:%d ...", args.wyoming_host, args.wyoming_port)
        silence = b"\x00" * (WYOMING_SAMPLE_RATE * WYOMING_SAMPLE_WIDTH)
        try:
            text = asyncio.run(wyoming_transcribe(args.wyoming_host, args.wyoming_port, silence, language=args.language, timeout=args.timeout))
        except Exception as error:
            LOG.error("Self-test failed: %s", error)
            raise SystemExit(1)
        LOG.info("Self-test succeeded -- the server responded with transcript: %r", text)
        LOG.info("(An empty string is normal for silence; the point is that the round trip worked.)")
        return

    check_wyoming_reachable(args.wyoming_host, args.wyoming_port)

    BridgeHandler.wyoming_host = args.wyoming_host
    BridgeHandler.wyoming_port = args.wyoming_port
    BridgeHandler.forced_language = args.language
    BridgeHandler.request_timeout = args.timeout

    server = ThreadingHTTPServer((args.listen_host, args.listen_port), BridgeHandler)
    LOG.info("Wyoming bridge listening on http://%s:%d", args.listen_host, args.listen_port)
    LOG.info("Forwarding transcription requests to Wyoming server at %s:%d", args.wyoming_host, args.wyoming_port)
    LOG.info("In TypeWhisper, set the 'OpenAI Compatible' engine's base URL to: http://%s:%d/v1", args.listen_host, args.listen_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
