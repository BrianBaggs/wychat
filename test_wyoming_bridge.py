"""Tests for wyoming_bridge.py.

Networking is real (real sockets, real asyncio, a real HTTP server) wherever practical --
loopback connections are fast and this is the code most likely to have subtle bugs (buffer
boundaries, event ordering). System-mutating calls (launchctl, schtasks, pbcopy/clip, and
anything touching the real home directory) are always mocked or redirected into tmp_path;
no test may install a real background service or write outside its own temp directory.
"""

import asyncio
import functools
import json
import logging
import socket
import subprocess
import sys
import threading
import wave
from http.client import HTTPConnection
from io import BytesIO
from typing import cast

import pytest

import wyoming_bridge as wb


def run_async(coro_func):
    """Lets an `async def test_*` run under plain (sync) pytest, no pytest-asyncio needed."""
    @functools.wraps(coro_func)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_func(*args, **kwargs))
    return wrapper


class FakeWriter:
    """Stands in for asyncio.StreamWriter for pure _write_event unit tests."""

    def __init__(self):
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        pass


def as_stream_writer(writer: "FakeWriter") -> asyncio.StreamWriter:
    """_write_event only needs .write()/.drain(); this satisfies the type checker for that duck type."""
    return cast(asyncio.StreamWriter, writer)


def make_reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def encode_event(event_type, data=None, payload=None) -> bytes:
    """Independent re-implementation of the wire format, for building test fixtures."""
    header = {"type": event_type}
    data_bytes = None
    if data:
        data_bytes = json.dumps(data).encode("utf-8")
        header["data_length"] = len(data_bytes)
    if payload:
        header["payload_length"] = len(payload)
    out = json.dumps(header).encode("utf-8") + b"\n"
    if data_bytes:
        out += data_bytes
    if payload:
        out += payload
    return out


class ScriptedWyomingServer:
    """A minimal, independent (blocking-socket) Wyoming test double for asyncio.open_connection
    clients to connect to over real loopback TCP. `on_events` receives the list of (type, data,
    payload) tuples received on a connection and returns a list of raw response bytes to send
    back (each already encoded via encode_event), or it may just close without responding.
    """

    def __init__(self, on_events):
        self._on_events = on_events
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._closed = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        f = conn.makefile("rwb")
        events = []
        try:
            while True:
                line = f.readline()
                if not line:
                    break
                header = json.loads(line)
                data = {}
                if header.get("data_length"):
                    data = json.loads(f.read(header["data_length"]))
                payload = None
                if header.get("payload_length"):
                    payload = f.read(header["payload_length"])
                events.append((header["type"], data, payload))
                if header["type"] == "audio-stop" or header["type"] == "describe":
                    for response in self._on_events(events):
                        f.write(response)
                    f.flush()
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            f.close()
            conn.close()

    def close(self):
        self._closed = True
        self._sock.close()


def respond_with_transcript(text):
    def on_events(_events):
        return [encode_event("transcript", data={"text": text})]
    return on_events


def close_without_responding(_events):
    return []


# ---------------------------------------------------------------------------
# _write_event / _read_event -- pure wire-format unit tests
# ---------------------------------------------------------------------------

@run_async
async def test_write_event_header_only_has_no_length_fields():
    writer = FakeWriter()
    await wb._write_event(as_stream_writer(writer), "audio-stop")
    line, rest = bytes(writer.buffer).split(b"\n", 1)
    header = json.loads(line)
    assert header == {"type": "audio-stop"}
    assert rest == b""


@run_async
async def test_write_event_empty_data_dict_omits_data_length():
    writer = FakeWriter()
    await wb._write_event(as_stream_writer(writer), "audio-stop", data={})
    header = json.loads(bytes(writer.buffer).split(b"\n", 1)[0])
    assert "data_length" not in header


@run_async
async def test_write_event_with_data_only():
    writer = FakeWriter()
    await wb._write_event(as_stream_writer(writer), "transcribe", data={"language": "en"})
    line, rest = bytes(writer.buffer).split(b"\n", 1)
    header = json.loads(line)
    assert header["data_length"] == len(rest)
    assert "payload_length" not in header
    assert json.loads(rest) == {"language": "en"}


@run_async
async def test_write_event_with_payload_only():
    writer = FakeWriter()
    await wb._write_event(as_stream_writer(writer), "audio-chunk", payload=b"abc")
    line, rest = bytes(writer.buffer).split(b"\n", 1)
    header = json.loads(line)
    assert "data_length" not in header
    assert header["payload_length"] == 3
    assert rest == b"abc"


@run_async
async def test_write_event_with_data_and_payload_are_ordered_correctly():
    writer = FakeWriter()
    data = {"rate": 16000, "width": 2, "channels": 1}
    await wb._write_event(as_stream_writer(writer), "audio-chunk", data=data, payload=b"PCM!")
    line, rest = bytes(writer.buffer).split(b"\n", 1)
    header = json.loads(line)
    data_bytes = json.dumps(data).encode("utf-8")
    assert rest == data_bytes + b"PCM!"
    assert header["data_length"] == len(data_bytes)
    assert header["payload_length"] == 4


@run_async
async def test_read_event_returns_none_on_eof():
    reader = make_reader(b"")
    assert await wb._read_event(reader) is None


@run_async
async def test_read_event_type_only():
    reader = make_reader(encode_event("audio-stop"))
    result = await wb._read_event(reader)
    assert result == ("audio-stop", {}, None)


@run_async
async def test_read_event_with_data():
    reader = make_reader(encode_event("transcript", data={"text": "hi"}))
    result = await wb._read_event(reader)
    assert result is not None
    event_type, data, payload = result
    assert event_type == "transcript"
    assert data == {"text": "hi"}
    assert payload is None


@run_async
async def test_read_event_with_payload():
    reader = make_reader(encode_event("audio-chunk", payload=b"xyz"))
    result = await wb._read_event(reader)
    assert result is not None
    _type, data, payload = result
    assert data == {}
    assert payload == b"xyz"


@run_async
async def test_read_event_with_data_and_payload():
    reader = make_reader(encode_event("audio-chunk", data={"rate": 16000}, payload=b"xyz"))
    result = await wb._read_event(reader)
    assert result is not None
    event_type, data, payload = result
    assert event_type == "audio-chunk"
    assert data == {"rate": 16000}
    assert payload == b"xyz"


@run_async
async def test_read_event_explicit_zero_lengths_are_treated_as_absent():
    header = json.dumps({"type": "audio-stop", "data_length": 0, "payload_length": 0}).encode() + b"\n"
    reader = make_reader(header)
    result = await wb._read_event(reader)
    assert result is not None
    event_type, data, payload = result
    assert event_type == "audio-stop"
    assert data == {}
    assert payload is None


# ---------------------------------------------------------------------------
# wyoming_transcribe / wyoming_describe -- real loopback socket integration
# ---------------------------------------------------------------------------

@run_async
async def test_wyoming_transcribe_success():
    server = ScriptedWyomingServer(respond_with_transcript("hello world"))
    try:
        text = await wb.wyoming_transcribe("127.0.0.1", server.port, b"\x00" * 100, timeout=5)
        assert text == "hello world"
    finally:
        server.close()


@run_async
async def test_wyoming_transcribe_sends_transcribe_event_first_when_language_given():
    received = []

    def on_events(events):
        received.extend(events)
        return [encode_event("transcript", data={"text": ""})]

    server = ScriptedWyomingServer(on_events)
    try:
        await wb.wyoming_transcribe("127.0.0.1", server.port, b"\x00" * 10, language="en", timeout=5)
    finally:
        server.close()
    assert received[0][0] == "transcribe"
    assert received[0][1] == {"language": "en"}
    assert received[1][0] == "audio-start"


@run_async
async def test_wyoming_transcribe_skips_audio_start_event_send_without_language():
    received = []

    def on_events(events):
        received.extend(events)
        return [encode_event("transcript", data={"text": ""})]

    server = ScriptedWyomingServer(on_events)
    try:
        await wb.wyoming_transcribe("127.0.0.1", server.port, b"\x00" * 10, language=None, timeout=5)
    finally:
        server.close()
    assert received[0][0] == "audio-start"


@run_async
async def test_wyoming_transcribe_ignores_non_transcript_events_before_the_real_one():
    def on_events(_events):
        return [
            encode_event("info", data={"noise": True}),
            encode_event("transcript", data={"text": "real answer"}),
        ]

    server = ScriptedWyomingServer(on_events)
    try:
        text = await wb.wyoming_transcribe("127.0.0.1", server.port, b"\x00" * 10, timeout=5)
    finally:
        server.close()
    assert text == "real answer"


@run_async
async def test_wyoming_transcribe_raises_when_server_closes_without_transcript():
    server = ScriptedWyomingServer(close_without_responding)
    try:
        with pytest.raises(ConnectionError):
            await wb.wyoming_transcribe("127.0.0.1", server.port, b"\x00" * 10, timeout=5)
    finally:
        server.close()


@run_async
async def test_wyoming_transcribe_chunks_audio_at_the_configured_boundary():
    received_chunk_lengths = []

    def on_events(events):
        received_chunk_lengths.extend(
            len(payload) for (etype, _data, payload) in events if etype == "audio-chunk"
        )
        return [encode_event("transcript", data={"text": "ok"})]

    server = ScriptedWyomingServer(on_events)
    audio = b"\x01" * (wb.AUDIO_CHUNK_BYTES * 2 + 100)
    try:
        await wb.wyoming_transcribe("127.0.0.1", server.port, audio, timeout=5)
    finally:
        server.close()
    assert received_chunk_lengths == [wb.AUDIO_CHUNK_BYTES, wb.AUDIO_CHUNK_BYTES, 100]


@run_async
async def test_wyoming_transcribe_swallows_wait_closed_errors(monkeypatch):
    async def raise_on_wait_closed(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", raise_on_wait_closed)
    server = ScriptedWyomingServer(respond_with_transcript("fine"))
    try:
        text = await wb.wyoming_transcribe("127.0.0.1", server.port, b"\x00" * 10, timeout=5)
        assert text == "fine"
    finally:
        server.close()


@run_async
async def test_wyoming_describe_success():
    def on_events(_events):
        return [encode_event("info", data={"asr": []})]

    server = ScriptedWyomingServer(on_events)
    try:
        result = await wb.wyoming_describe("127.0.0.1", server.port, timeout=5)
    finally:
        server.close()
    assert result == "info"


@run_async
async def test_wyoming_describe_raises_when_server_closes_without_responding():
    server = ScriptedWyomingServer(close_without_responding)
    try:
        with pytest.raises(ConnectionError):
            await wb.wyoming_describe("127.0.0.1", server.port, timeout=5)
    finally:
        server.close()


@run_async
async def test_wyoming_describe_swallows_wait_closed_errors(monkeypatch):
    async def raise_on_wait_closed(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", raise_on_wait_closed)

    def on_events(_events):
        return [encode_event("info")]

    server = ScriptedWyomingServer(on_events)
    try:
        result = await wb.wyoming_describe("127.0.0.1", server.port, timeout=5)
        assert result == "info"
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Audio handling
# ---------------------------------------------------------------------------

def make_wav_bytes(rate=16000, width=2, channels=1, frames=b"\x00\x00" * 10) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setframerate(rate)
        wav_file.setsampwidth(width)
        wav_file.setnchannels(channels)
        wav_file.writeframes(frames)
    return buf.getvalue()


def test_pcm_from_upload_routes_riff_magic_bytes_to_wav_extraction(monkeypatch):
    calls = []
    monkeypatch.setattr(wb, "_extract_wav_pcm", lambda b: calls.append(b) or b"OUT")
    monkeypatch.setattr(wb, "_transcode_with_ffmpeg", lambda b: pytest.fail("should not be called"))
    result = wb.pcm_from_upload(b"RIFF....WAVE", filename=None, content_type=None)
    assert result == b"OUT"
    assert calls == [b"RIFF....WAVE"]


@pytest.mark.parametrize("content_type", ["audio/wav", "audio/x-wav", "audio/wave", "AUDIO/WAV"])
def test_pcm_from_upload_routes_by_content_type(monkeypatch, content_type):
    monkeypatch.setattr(wb, "_extract_wav_pcm", lambda b: b"OUT")
    monkeypatch.setattr(wb, "_transcode_with_ffmpeg", lambda b: pytest.fail("should not be called"))
    result = wb.pcm_from_upload(b"not-really-wav-bytes", filename=None, content_type=content_type)
    assert result == b"OUT"


def test_pcm_from_upload_routes_by_filename_extension(monkeypatch):
    monkeypatch.setattr(wb, "_extract_wav_pcm", lambda b: b"OUT")
    monkeypatch.setattr(wb, "_transcode_with_ffmpeg", lambda b: pytest.fail("should not be called"))
    result = wb.pcm_from_upload(b"not-really-wav-bytes", filename="Recording.WAV", content_type=None)
    assert result == b"OUT"


def test_pcm_from_upload_falls_back_to_ffmpeg_when_nothing_indicates_wav(monkeypatch):
    calls = []
    monkeypatch.setattr(wb, "_extract_wav_pcm", lambda b: pytest.fail("should not be called"))
    monkeypatch.setattr(wb, "_transcode_with_ffmpeg", lambda b: calls.append(b) or b"OUT")
    result = wb.pcm_from_upload(b"\x00\x01\x02", filename="clip.m4a", content_type="audio/mp4")
    assert result == b"OUT"
    assert calls == [b"\x00\x01\x02"]


def test_pcm_from_upload_handles_none_filename_and_content_type(monkeypatch):
    monkeypatch.setattr(wb, "_transcode_with_ffmpeg", lambda b: b"OUT")
    result = wb.pcm_from_upload(b"\x00\x01\x02", filename=None, content_type=None)
    assert result == b"OUT"


def test_extract_wav_pcm_returns_frames_directly_when_format_matches(monkeypatch):
    monkeypatch.setattr(wb, "_transcode_with_ffmpeg", lambda b: pytest.fail("should not be called"))
    frames = b"\x11\x22" * 50
    wav_bytes = make_wav_bytes(rate=16000, width=2, channels=1, frames=frames)
    assert wb._extract_wav_pcm(wav_bytes) == frames


def test_extract_wav_pcm_transcodes_when_format_does_not_match(monkeypatch):
    calls = []
    monkeypatch.setattr(wb, "_transcode_with_ffmpeg", lambda b: calls.append(b) or b"TRANSCODED")
    wav_bytes = make_wav_bytes(rate=8000, width=2, channels=1)
    result = wb._extract_wav_pcm(wav_bytes)
    assert result == b"TRANSCODED"
    assert calls == [wav_bytes]


def test_transcode_with_ffmpeg_raises_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(wb.shutil, "which", lambda name: None)
    with pytest.raises(wb.UnsupportedAudioError):
        wb._transcode_with_ffmpeg(b"anything")


def test_transcode_with_ffmpeg_returns_stdout_on_success(monkeypatch):
    monkeypatch.setattr(wb.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        wb.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=b"PCM-DATA", stderr=b""),
    )
    assert wb._transcode_with_ffmpeg(b"anything") == b"PCM-DATA"


def test_transcode_with_ffmpeg_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(wb.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        wb.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=b"", stderr=b"bad input"),
    )
    with pytest.raises(wb.UnsupportedAudioError, match="bad input"):
        wb._transcode_with_ffmpeg(b"anything")


def test_transcode_with_ffmpeg_raises_on_empty_stdout(monkeypatch):
    monkeypatch.setattr(wb.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        wb.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=b"", stderr=b""),
    )
    with pytest.raises(wb.UnsupportedAudioError):
        wb._transcode_with_ffmpeg(b"anything")


# ---------------------------------------------------------------------------
# parse_multipart / _parse_content_disposition
# ---------------------------------------------------------------------------

def build_multipart_body(boundary: bytes, parts) -> bytes:
    out = bytearray()
    for headers, content in parts:
        out += b"--" + boundary + b"\r\n"
        out += headers + b"\r\n\r\n"
        out += content + b"\r\n"
    out += b"--" + boundary + b"--\r\n"
    return bytes(out)


def test_parse_multipart_extracts_file_and_text_fields():
    boundary = b"BOUND123"
    file_headers = b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav'
    body = build_multipart_body(boundary, [
        (file_headers, b"\x00\x01BINARY\r\nwithembeddedcrlf"),
        (b'Content-Disposition: form-data; name="language"', b"en"),
    ])
    fields = wb.parse_multipart(body, boundary)
    assert fields["file"].filename == "audio.wav"
    assert fields["file"].content_type == "audio/wav"
    assert fields["file"].content == b"\x00\x01BINARY\r\nwithembeddedcrlf"
    assert fields["language"].content == b"en"
    assert fields["language"].filename is None


def test_parse_multipart_skips_chunk_missing_header_body_separator():
    boundary = b"BOUND123"
    good = b"--" + boundary + b"\r\n" + b'Content-Disposition: form-data; name="a"\r\n\r\nvalue\r\n'
    malformed = b"--" + boundary + b"\r\nno double crlf here at all\r\n"
    closing = b"--" + boundary + b"--\r\n"
    body = good + malformed + closing
    fields = wb.parse_multipart(body, boundary)
    assert set(fields) == {"a"}


def test_parse_multipart_skips_chunk_without_crlf_framing():
    boundary = b"BOUND123"
    good = b"--" + boundary + b"\r\n" + b'Content-Disposition: form-data; name="a"\r\n\r\nvalue\r\n'
    unframed = b"--" + boundary + b"NOFRAMINGHERE"
    closing = b"--" + boundary + b"--\r\n"
    body = good + unframed + closing
    fields = wb.parse_multipart(body, boundary)
    assert set(fields) == {"a"}


def test_parse_multipart_skips_field_with_no_name():
    boundary = b"BOUND123"
    body = build_multipart_body(boundary, [
        (b"Content-Disposition: form-data", b"orphan"),
    ])
    fields = wb.parse_multipart(body, boundary)
    assert fields == {}


def test_parse_multipart_skips_header_line_without_colon():
    boundary = b"BOUND123"
    body = build_multipart_body(boundary, [
        (b'garbage-header-no-colon\r\nContent-Disposition: form-data; name="a"', b"value"),
    ])
    fields = wb.parse_multipart(body, boundary)
    assert fields["a"].content == b"value"


def test_parse_multipart_with_no_boundary_occurrences_returns_empty():
    assert wb.parse_multipart(b"nothing to see here", b"BOUND123") == {}


@pytest.mark.parametrize(
    "value,expected",
    [
        ('form-data; name="file"', ("file", None)),
        ('form-data; name="file"; filename="a.wav"', ("file", "a.wav")),
        ("form-data", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_content_disposition(value, expected):
    assert wb._parse_content_disposition(value) == expected


# ---------------------------------------------------------------------------
# BridgeHandler -- real HTTP server over loopback
# ---------------------------------------------------------------------------

class RunningBridge:
    def __init__(self, fake_wyoming):
        wb.BridgeHandler.wyoming_host = "127.0.0.1"
        wb.BridgeHandler.wyoming_port = fake_wyoming.port if fake_wyoming else 1
        wb.BridgeHandler.forced_language = None
        wb.BridgeHandler.request_timeout = 5.0
        self.server = wb.ThreadingHTTPServer(("127.0.0.1", 0), wb.BridgeHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, json.loads(payload) if payload else None
        finally:
            conn.close()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def bridge():
    fake = ScriptedWyomingServer(respond_with_transcript("received ok"))
    running = RunningBridge(fake)
    yield running
    running.close()
    fake.close()


def multipart_request_body(fields: dict, file_field=None):
    boundary = "TESTBOUND456"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    if file_field is not None:
        filename, content_type, content = file_field
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n'.encode() + content + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def post_transcription(bridge, body, content_type):
    return bridge.request(
        "POST", "/v1/audio/transcriptions", body=body, headers={"Content-Type": content_type}
    )


def test_do_get_root(bridge):
    status, payload = bridge.request("GET", "/")
    assert status == 200
    assert payload == {"status": "ok", "bridge": "wyoming-whisper"}


def test_do_get_v1_models(bridge):
    status, payload = bridge.request("GET", "/v1/models")
    assert status == 200
    assert payload["object"] == "list"
    assert payload["data"][0]["id"] == "wyoming-whisper"


def test_do_get_v1_models_with_trailing_slash_and_query(bridge):
    status, payload = bridge.request("GET", "/v1/models/?extra=1")
    assert status == 200
    assert payload["object"] == "list"


def test_do_post_unknown_path_is_404(bridge):
    body, content_type = multipart_request_body({}, file_field=("a.wav", "audio/wav", make_wav_bytes()))
    status, payload = bridge.request("POST", "/something-else", body=body, headers={"Content-Type": content_type})
    assert status == 404


def test_do_post_translate_path_is_400(bridge):
    body, content_type = multipart_request_body({}, file_field=("a.wav", "audio/wav", make_wav_bytes()))
    status, payload = bridge.request(
        "POST", "/v1/audio/translations", body=body, headers={"Content-Type": content_type}
    )
    assert status == 400
    assert "translation" in payload["error"]


def test_do_post_without_multipart_content_type_is_400(bridge):
    status, payload = bridge.request(
        "POST", "/v1/audio/transcriptions", body=b"{}", headers={"Content-Type": "application/json"}
    )
    assert status == 400
    assert "multipart" in payload["error"]


def test_do_post_missing_file_field_is_400(bridge):
    body, content_type = multipart_request_body({"model": "whisper-1"})
    status, payload = post_transcription(bridge, body, content_type)
    assert status == 400
    assert "file" in payload["error"]


def test_do_post_success_uses_per_request_language(bridge):
    body, content_type = multipart_request_body(
        {"model": "whisper-1", "language": "en"}, file_field=("a.wav", "audio/wav", make_wav_bytes())
    )
    status, payload = post_transcription(bridge, body, content_type)
    assert status == 200
    assert payload["text"] == "received ok"


def test_do_post_forced_language_overrides_request_field(bridge):
    wb.BridgeHandler.forced_language = "fr"
    try:
        body, content_type = multipart_request_body(
            {"language": "en"}, file_field=("a.wav", "audio/wav", make_wav_bytes())
        )
        status, _payload = post_transcription(bridge, body, content_type)
        assert status == 200
    finally:
        wb.BridgeHandler.forced_language = None


def test_do_post_unsupported_audio_is_415(bridge, monkeypatch):
    monkeypatch.setattr(wb.shutil, "which", lambda name: None)
    body, content_type = multipart_request_body({}, file_field=("clip.m4a", "audio/mp4", b"\x00\x01\x02not really wav"))
    status, payload = post_transcription(bridge, body, content_type)
    assert status == 415
    assert "ffmpeg" in payload["error"]


def test_do_post_wyoming_unreachable_is_502(bridge):
    wb.BridgeHandler.wyoming_port = 1  # nothing listens on a privileged port we didn't bind
    body, content_type = multipart_request_body({}, file_field=("a.wav", "audio/wav", make_wav_bytes()))
    status, payload = post_transcription(bridge, body, content_type)
    assert status == 502
    assert "Wyoming server error" in payload["error"]


def test_log_message_is_emitted_for_requests(bridge, caplog):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")
    bridge.request("GET", "/")
    assert any("GET / HTTP/1.1" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# normalize_host
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("192.168.7.55", "192.168.7.55"),
        ("  192.168.7.55  ", "192.168.7.55"),
        ("http://192.168.7.55", "192.168.7.55"),
        ("https://192.168.7.55", "192.168.7.55"),
        ("tcp://192.168.7.55", "192.168.7.55"),
        ("192.168.7.55/some/path", "192.168.7.55"),
        ("192.168.7.55:10300", "192.168.7.55"),
        ("http://192.168.7.55:10300", "192.168.7.55"),
        (":10300", ":10300"),
        ("homeassistant.local:notaport", "homeassistant.local:notaport"),
        ("homeassistant.local", "homeassistant.local"),
    ],
)
def test_normalize_host(raw, expected):
    assert wb.normalize_host(raw) == expected


def test_normalize_host_warns_on_scheme_and_port(caplog):
    caplog.set_level(logging.WARNING, logger="wyoming_bridge")
    wb.normalize_host("http://192.168.7.55:10300")
    messages = " ".join(r.message for r in caplog.records)
    assert "prefix" in messages
    assert "port attached" in messages


# ---------------------------------------------------------------------------
# check_wyoming_reachable
# ---------------------------------------------------------------------------

def test_check_wyoming_reachable_when_open(caplog):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")
    server = ScriptedWyomingServer(close_without_responding)
    try:
        wb.check_wyoming_reachable("127.0.0.1", server.port, timeout=2)
    finally:
        server.close()
    assert any("is reachable" in r.message for r in caplog.records)


def test_check_wyoming_reachable_when_closed_does_not_raise(caplog):
    caplog.set_level(logging.WARNING, logger="wyoming_bridge")
    wb.check_wyoming_reachable("127.0.0.1", 1, timeout=1)
    assert any("Could not reach" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# base_url_for / save_config / copy_to_clipboard / _service_command_args
# ---------------------------------------------------------------------------

def test_base_url_for():
    assert wb.base_url_for("127.0.0.1", 8765) == "http://127.0.0.1:8765/v1"


def make_args(**overrides):
    defaults = {
        "wyoming_host": "192.168.7.47", "wyoming_port": 10300,
        "listen_host": "127.0.0.1", "listen_port": 8765,
        "language": None, "timeout": 60.0,
    }
    defaults.update(overrides)
    return wb.argparse.Namespace(**defaults)


def test_save_config_writes_expected_fields(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(wb, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wb, "CONFIG_PATH", config_path)
    args = make_args(language="en")
    result_path = wb.save_config(args)
    assert result_path == config_path
    saved = json.loads(config_path.read_text())
    assert saved == {
        "base_url": "http://127.0.0.1:8765/v1",
        "wyoming_host": "192.168.7.47",
        "wyoming_port": 10300,
        "listen_host": "127.0.0.1",
        "listen_port": 8765,
        "language": "en",
    }


@pytest.mark.parametrize("system,command", [("Darwin", "pbcopy"), ("Windows", "clip")])
def test_copy_to_clipboard_success(monkeypatch, system, command):
    monkeypatch.setattr(wb.platform, "system", lambda: system)
    calls = []
    monkeypatch.setattr(wb.subprocess, "run", lambda argv, **k: calls.append((argv, k)))
    assert wb.copy_to_clipboard("http://x") is True
    assert calls[0][0] == [command]
    assert calls[0][1]["input"] == b"http://x"


def test_copy_to_clipboard_unsupported_platform_does_not_call_subprocess(monkeypatch):
    monkeypatch.setattr(wb.platform, "system", lambda: "Linux")
    calls = []
    monkeypatch.setattr(wb.subprocess, "run", lambda *a, **k: calls.append(a))
    assert wb.copy_to_clipboard("http://x") is False
    assert calls == []


@pytest.mark.parametrize("error", [FileNotFoundError("no pbcopy"), subprocess.CalledProcessError(1, ["pbcopy"])])
def test_copy_to_clipboard_swallows_errors(monkeypatch, error):
    monkeypatch.setattr(wb.platform, "system", lambda: "Darwin")

    def raise_error(*_a, **_k):
        raise error

    monkeypatch.setattr(wb.subprocess, "run", raise_error)
    assert wb.copy_to_clipboard("http://x") is False


def test_service_command_args_without_language():
    args = make_args(language=None)
    command = wb._service_command_args(args)
    assert command[0] == sys.executable
    assert "--language" not in command
    assert "--wyoming-host" in command
    assert command[command.index("--wyoming-host") + 1] == "192.168.7.47"


def test_service_command_args_with_language():
    args = make_args(language="en")
    command = wb._service_command_args(args)
    assert command[-2:] == ["--language", "en"]


# ---------------------------------------------------------------------------
# launchd / Windows Task Scheduler install+uninstall -- fully mocked, no real
# system mutation: paths are redirected into tmp_path and subprocess.run never
# touches the real launchctl/schtasks/filesystem outside of it.
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_service_paths(monkeypatch, tmp_path):
    plist_path = tmp_path / "LaunchAgents" / f"{wb.SERVICE_LABEL}.plist"
    log_path = tmp_path / "Logs" / "wyoming-bridge.log"
    bat_path = tmp_path / "run_wyoming_bridge.bat"
    monkeypatch.setattr(wb, "_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(wb, "_launchd_log_path", lambda: log_path)
    monkeypatch.setattr(wb, "_windows_wrapper_bat_path", lambda: bat_path)
    monkeypatch.setattr(wb, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wb, "CONFIG_PATH", tmp_path / "config.json")
    return {"plist": plist_path, "log": log_path, "bat": bat_path}


def test_launchd_plist_path_shape():
    # Pure Path arithmetic, no filesystem access -- safe to call unmocked.
    path = wb._launchd_plist_path()
    assert path.name == f"{wb.SERVICE_LABEL}.plist"
    assert path.parent.name == "LaunchAgents"


def test_launchd_log_path_shape():
    path = wb._launchd_log_path()
    assert path.name == "wyoming-bridge.log"
    assert path.parent.name == "Logs"


def test_windows_wrapper_bat_path_shape():
    path = wb._windows_wrapper_bat_path()
    assert path.name == "run_wyoming_bridge.bat"
    assert path.parent == wb.CONFIG_DIR


def fake_run_returning(returncode_for):
    """Builds a subprocess.run replacement: returncode_for(argv) -> int."""
    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, returncode_for(argv), stdout=b"", stderr=b"boom")
    return fake_run


def test_install_launchd_success_writes_plist(monkeypatch, isolated_service_paths):
    monkeypatch.setattr(wb.subprocess, "run", fake_run_returning(lambda argv: 0))
    args = make_args()
    wb._install_launchd(args)
    with open(isolated_service_paths["plist"], "rb") as f:
        plist = wb.plistlib.load(f)
    assert plist["Label"] == wb.SERVICE_LABEL
    assert plist["ProgramArguments"] == wb._service_command_args(args)
    assert plist["RunAtLoad"] is True


def test_install_launchd_raises_on_load_failure(monkeypatch, isolated_service_paths):
    monkeypatch.setattr(wb.subprocess, "run", fake_run_returning(lambda argv: 1 if "load" in argv else 0))
    with pytest.raises(SystemExit):
        wb._install_launchd(make_args())


def test_uninstall_launchd_removes_existing_plist(monkeypatch, isolated_service_paths, caplog):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")
    isolated_service_paths["plist"].parent.mkdir(parents=True, exist_ok=True)
    isolated_service_paths["plist"].write_bytes(b"placeholder")
    monkeypatch.setattr(wb.subprocess, "run", fake_run_returning(lambda argv: 0))
    wb._uninstall_launchd()
    assert not isolated_service_paths["plist"].exists()
    assert any("Removed and unloaded" in r.message for r in caplog.records)


def test_uninstall_launchd_when_nothing_installed(monkeypatch, isolated_service_paths, caplog):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")
    monkeypatch.setattr(wb.subprocess, "run", fake_run_returning(lambda argv: 0))
    wb._uninstall_launchd()
    assert any("No launchd service" in r.message for r in caplog.records)


def test_install_windows_task_success_writes_bat(monkeypatch, isolated_service_paths):
    monkeypatch.setattr(wb.subprocess, "run", fake_run_returning(lambda argv: 0))
    args = make_args()
    wb._install_windows_task(args)
    # Read as raw bytes -- Path.read_text() would silently normalize the \r\n
    # line endings a .bat file actually needs, masking a real regression.
    content = isolated_service_paths["bat"].read_bytes()
    assert content.startswith(b"@echo off\r\n")
    assert subprocess.list2cmdline(wb._service_command_args(args)).encode("utf-8") in content


def test_install_windows_task_raises_on_create_failure(monkeypatch, isolated_service_paths):
    monkeypatch.setattr(wb.subprocess, "run", fake_run_returning(lambda argv: 1))
    with pytest.raises(SystemExit):
        wb._install_windows_task(make_args())


def test_uninstall_windows_task_success(monkeypatch, isolated_service_paths, caplog):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")
    isolated_service_paths["bat"].write_text("placeholder")
    monkeypatch.setattr(wb.subprocess, "run", fake_run_returning(lambda argv: 0))
    wb._uninstall_windows_task()
    assert not isolated_service_paths["bat"].exists()
    assert any("Removed the scheduled task" in r.message for r in caplog.records)


def test_uninstall_windows_task_reports_when_nothing_installed(monkeypatch, isolated_service_paths, caplog):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")
    monkeypatch.setattr(wb.subprocess, "run", fake_run_returning(lambda argv: 1))
    wb._uninstall_windows_task()
    assert any("No scheduled task" in r.message for r in caplog.records)


@pytest.mark.parametrize("system,target", [("Darwin", "_install_launchd"), ("Windows", "_install_windows_task")])
def test_install_service_dispatches_by_platform(monkeypatch, system, target):
    monkeypatch.setattr(wb.platform, "system", lambda: system)
    calls = []
    monkeypatch.setattr(wb, target, lambda args: calls.append(args))
    args = make_args()
    wb.install_service(args)
    assert calls == [args]


def test_install_service_unsupported_platform_raises(monkeypatch):
    monkeypatch.setattr(wb.platform, "system", lambda: "Linux")
    with pytest.raises(SystemExit):
        wb.install_service(make_args())


@pytest.mark.parametrize("system,target", [("Darwin", "_uninstall_launchd"), ("Windows", "_uninstall_windows_task")])
def test_uninstall_service_dispatches_by_platform(monkeypatch, system, target):
    monkeypatch.setattr(wb.platform, "system", lambda: system)
    calls = []
    monkeypatch.setattr(wb, target, lambda: calls.append(True))
    wb.uninstall_service()
    assert calls == [True]


def test_uninstall_service_unsupported_platform_raises(monkeypatch):
    monkeypatch.setattr(wb.platform, "system", lambda: "Linux")
    with pytest.raises(SystemExit):
        wb.uninstall_service()


# ---------------------------------------------------------------------------
# Interactive setup wizard
# ---------------------------------------------------------------------------

def scripted_input(monkeypatch, answers):
    queue = list(answers)

    def fake_input(_prompt=""):
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def test_prompt_returns_typed_answer(monkeypatch):
    scripted_input(monkeypatch, ["hello"])
    assert wb._prompt("Question", default="fallback") == "hello"


def test_prompt_returns_default_when_blank(monkeypatch):
    scripted_input(monkeypatch, [""])
    assert wb._prompt("Question", default="fallback") == "fallback"


@pytest.mark.parametrize(
    "answer,default,expected",
    [
        ("", False, False),
        ("", True, True),
        ("y", False, True),
        ("yes", False, True),
        ("Y", False, True),
        ("n", True, False),
        ("anything-else", True, False),
    ],
)
def test_prompt_yes_no(monkeypatch, answer, default, expected):
    scripted_input(monkeypatch, [answer])
    assert wb._prompt_yes_no("Question?", default=default) is expected


@pytest.mark.parametrize(
    "system,which_map,expected",
    [
        ("Darwin", {"brew": "/opt/homebrew/bin/brew"}, ("brew", "install", "ffmpeg")),
        ("Darwin", {}, None),
        ("Windows", {"winget": "C:\\winget.exe"}, ("winget", "install", "ffmpeg")),
        ("Windows", {}, None),
        ("Linux", {"brew": "/usr/bin/brew"}, None),
    ],
)
def test_detect_package_manager(monkeypatch, system, which_map, expected):
    monkeypatch.setattr(wb.platform, "system", lambda: system)
    monkeypatch.setattr(wb.shutil, "which", lambda name: which_map.get(name))
    assert wb._detect_package_manager() == expected


def test_offer_ffmpeg_install_skips_when_already_installed(monkeypatch):
    monkeypatch.setattr(wb.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    calls = []
    monkeypatch.setattr(wb.subprocess, "run", lambda *a, **k: calls.append(a))
    wb._offer_ffmpeg_install()
    assert calls == []


def test_offer_ffmpeg_install_prints_manual_link_without_package_manager(monkeypatch, capsys):
    monkeypatch.setattr(wb.shutil, "which", lambda name: None)
    monkeypatch.setattr(wb, "_detect_package_manager", lambda: None)
    calls = []
    monkeypatch.setattr(wb.subprocess, "run", lambda *a, **k: calls.append(a))
    wb._offer_ffmpeg_install()
    assert calls == []
    assert "ffmpeg.org" in capsys.readouterr().out


def test_offer_ffmpeg_install_runs_installer_when_confirmed(monkeypatch):
    monkeypatch.setattr(wb.shutil, "which", lambda name: None)
    monkeypatch.setattr(wb, "_detect_package_manager", lambda: ("brew", "install", "ffmpeg"))
    scripted_input(monkeypatch, ["y"])
    calls = []
    monkeypatch.setattr(wb.subprocess, "run", lambda argv, **k: calls.append(argv))
    wb._offer_ffmpeg_install()
    assert calls == [("brew", "install", "ffmpeg")]


def test_offer_ffmpeg_install_skips_installer_when_declined(monkeypatch):
    monkeypatch.setattr(wb.shutil, "which", lambda name: None)
    monkeypatch.setattr(wb, "_detect_package_manager", lambda: ("brew", "install", "ffmpeg"))
    scripted_input(monkeypatch, ["n"])
    calls = []
    monkeypatch.setattr(wb.subprocess, "run", lambda *a, **k: calls.append(a))
    wb._offer_ffmpeg_install()
    assert calls == []


def test_run_setup_wizard_reprompts_until_host_given(monkeypatch, tmp_path):
    monkeypatch.setattr(wb, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wb, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(wb, "check_wyoming_reachable", lambda host, port: None)
    monkeypatch.setattr(wb, "_offer_ffmpeg_install", lambda: None)
    scripted_input(monkeypatch, ["", "192.168.7.47", "", "", "", "n"])
    initial = make_args(wyoming_host=None)
    wb.run_setup_wizard(initial)
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["wyoming_host"] == "192.168.7.47"


def test_run_setup_wizard_installs_service_and_copies_url(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(wb, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wb, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(wb, "check_wyoming_reachable", lambda host, port: None)
    monkeypatch.setattr(wb, "_offer_ffmpeg_install", lambda: None)
    install_calls = []
    monkeypatch.setattr(wb, "install_service", lambda args: install_calls.append(args))
    monkeypatch.setattr(wb, "copy_to_clipboard", lambda text: True)
    # host, wyoming port (blank = default), listen port (blank = default), language (blank), install service? yes
    scripted_input(monkeypatch, ["192.168.7.47", "", "", "", "y"])
    wb.run_setup_wizard(make_args())
    assert len(install_calls) == 1
    assert install_calls[0].wyoming_host == "192.168.7.47"
    assert "Copied" in capsys.readouterr().out


def test_run_setup_wizard_installs_service_prints_url_when_clipboard_unavailable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(wb, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wb, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(wb, "check_wyoming_reachable", lambda host, port: None)
    monkeypatch.setattr(wb, "_offer_ffmpeg_install", lambda: None)
    monkeypatch.setattr(wb, "install_service", lambda args: None)
    monkeypatch.setattr(wb, "copy_to_clipboard", lambda text: False)
    scripted_input(monkeypatch, ["192.168.7.47", "", "", "", "y"])
    wb.run_setup_wizard(make_args())
    assert "TypeWhisper base URL" in capsys.readouterr().out


def test_run_setup_wizard_declines_service_prints_manual_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(wb, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wb, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(wb, "check_wyoming_reachable", lambda host, port: None)
    monkeypatch.setattr(wb, "_offer_ffmpeg_install", lambda: None)
    install_calls = []
    monkeypatch.setattr(wb, "install_service", lambda args: install_calls.append(args))
    scripted_input(monkeypatch, ["192.168.7.47", "", "", "en", "n"])
    wb.run_setup_wizard(make_args())
    assert install_calls == []
    out = capsys.readouterr().out
    assert "Run it with:" in out
    assert "TypeWhisper base URL" in out


# ---------------------------------------------------------------------------
# main() -- CLI wiring; underlying behavior is mocked since it's covered above
# ---------------------------------------------------------------------------

def run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["wyoming_bridge.py"] + argv)
    wb.main()


def test_main_uninstall_service_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(wb, "uninstall_service", lambda: calls.append(True))
    run_main(monkeypatch, ["--uninstall-service"])
    assert calls == [True]


def test_main_requires_wyoming_host_unless_uninstalling(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        run_main(monkeypatch, [])
    assert excinfo.value.code == 2


def test_main_setup_dispatch_does_not_require_wyoming_host(monkeypatch):
    calls = []
    monkeypatch.setattr(wb, "run_setup_wizard", lambda args: calls.append(args))
    run_main(monkeypatch, ["--setup"])
    assert len(calls) == 1
    assert calls[0].wyoming_host is None


def test_main_install_service_dispatch_copies_to_clipboard(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")
    monkeypatch.setattr(wb, "check_wyoming_reachable", lambda host, port: None)
    monkeypatch.setattr(wb, "save_config", lambda args: None)
    install_calls = []
    monkeypatch.setattr(wb, "install_service", lambda args: install_calls.append(args))
    monkeypatch.setattr(wb, "copy_to_clipboard", lambda text: True)
    run_main(monkeypatch, ["--wyoming-host", "192.168.7.47", "--install-service"])
    assert len(install_calls) == 1
    assert any("Copied" in r.message for r in caplog.records)


def test_main_install_service_dispatch_prints_url_when_clipboard_unavailable(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")
    monkeypatch.setattr(wb, "check_wyoming_reachable", lambda host, port: None)
    monkeypatch.setattr(wb, "save_config", lambda args: None)
    monkeypatch.setattr(wb, "install_service", lambda args: None)
    monkeypatch.setattr(wb, "copy_to_clipboard", lambda text: False)
    run_main(monkeypatch, ["--wyoming-host", "192.168.7.47", "--install-service"])
    assert any("TypeWhisper base URL" in r.message for r in caplog.records)


def test_main_selftest_success(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")

    async def fake_transcribe(*_a, **_k):
        return "hello"

    monkeypatch.setattr(wb, "wyoming_transcribe", fake_transcribe)
    run_main(monkeypatch, ["--wyoming-host", "192.168.7.47", "--selftest"])
    assert any("Self-test succeeded" in r.message for r in caplog.records)


def test_main_selftest_failure_exits(monkeypatch):
    async def fake_transcribe(*_a, **_k):
        raise ConnectionError("nope")

    monkeypatch.setattr(wb, "wyoming_transcribe", fake_transcribe)
    with pytest.raises(SystemExit) as excinfo:
        run_main(monkeypatch, ["--wyoming-host", "192.168.7.47", "--selftest"])
    assert excinfo.value.code == 1


def test_main_normal_run_serves_until_keyboard_interrupt(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.INFO, logger="wyoming_bridge")
    monkeypatch.setattr(wb, "check_wyoming_reachable", lambda host, port: None)
    monkeypatch.setattr(wb, "save_config", lambda args: tmp_path / "config.json")

    shutdown_calls = []

    class FakeServer:
        def __init__(self, address, handler_class):
            self.address = address
            self.handler_class = handler_class

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            shutdown_calls.append(True)

    monkeypatch.setattr(wb, "ThreadingHTTPServer", FakeServer)
    run_main(monkeypatch, ["--wyoming-host", "192.168.7.47", "--listen-port", "0"])
    assert shutdown_calls == [True]
    assert any("Shutting down" in r.message for r in caplog.records)
