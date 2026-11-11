import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cast2nest", ROOT / "cast2nest.py")
assert SPEC and SPEC.loader
cast2nest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cast2nest)

CAST_IP = "192.0.2.1"
OTHER_IP = "192.0.2.99"


def request(handler_cls, method, path, source_ip=OTHER_IP, headers=None):
    """Execute one handler without opening a socket or writing access logs."""
    handler = handler_cls.__new__(handler_cls)
    handler.directory = os.getcwd()
    handler.path = path
    handler.client_address = (source_ip, 12345)
    handler.command = method
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.headers = headers or {}
    handler.wfile = io.BytesIO()
    handler.close_connection = False
    handler.log_message = lambda *args: None
    handler.log_request = lambda *args: None

    getattr(handler, f"do_{method}")()
    raw = handler.wfile.getvalue()
    raw_headers, _, body = raw.partition(b"\r\n\r\n")
    lines = raw_headers.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    response_headers = {}
    for line in lines[1:]:
        name, value = line.decode("latin1").split(":", 1)
        response_headers[name.lower()] = value.strip()
    return status, response_headers, body


def monitor(messages):
    session = cast2nest.CastSession.__new__(cast2nest.CastSession)
    session.sock = object()
    session.send = lambda *args: None
    incoming = iter(messages)

    def recv_msg(**kwargs):
        try:
            return next(incoming)
        except StopIteration as exc:
            raise AssertionError(
                "monitor did not stop after the final message"
            ) from exc

    session.recv_msg = recv_msg
    with patch.object(
        cast2nest.select, "select", return_value=([session.sock], [], [])
    ):
        return session.monitor()


def media_status(player_state, idle_reason=None):
    status = {"playerState": player_state}
    if idle_reason:
        status["idleReason"] = idle_reason
    return {
        "ns": cast2nest.NS_MEDIA,
        "json": {"type": "MEDIA_STATUS", "status": [status]},
    }


class ProtocolTests(unittest.TestCase):
    def test_varint_round_trip(self):
        for number in (0, 1, 127, 128, 300, 2**32 - 1):
            with self.subTest(number=number):
                encoded = cast2nest.enc_varint(number)
                decoded, position = cast2nest.dec_varint(encoded, 0)
                self.assertEqual((decoded, position), (number, len(encoded)))

    def test_cast_message_round_trip(self):
        message = cast2nest.build_msg(
            "source", "destination", "namespace", utf8='{"type":"PING"}'
        )
        parsed = cast2nest.parse_msg(message)
        self.assertEqual(parsed["json"], {"type": "PING"})
        self.assertEqual(
            (parsed["src"], parsed["dst"], parsed["ns"]),
            ("source", "destination", "namespace"),
        )


class MediaTests(unittest.TestCase):
    def test_directory_expansion_filters_and_sorts_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("b.MP3", "a.flac", "ignore.txt"):
                Path(directory, name).touch()
            tracks = cast2nest.expand_media_args([directory])
        self.assertEqual(
            [Path(track["media"]).name for track in tracks], ["a.flac", "b.MP3"]
        )

    def test_cli_uses_project_name(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "cast2nest.py"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("usage: cast2nest.py", result.stdout)
        self.assertNotIn("cast_audio.py", result.stdout)


class RangeFileHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.file_path = Path(self.temp_dir.name, "audio.mp3")
        self.file_path.write_bytes(b"0123456789")
        self.handler = type(
            "Files",
            (cast2nest.RangeFileHandler,),
            {
                "routes": {"/media/audio.mp3": str(self.file_path)},
                "allowed_ip": CAST_IP,
            },
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_get_serves_only_a_registered_file_to_the_cast(self):
        status, _, body = request(self.handler, "GET", "/media/audio.mp3", CAST_IP)
        self.assertEqual((status, body), (200, b"0123456789"))

    def test_get_rejects_another_ip(self):
        status, _, _ = request(self.handler, "GET", "/media/audio.mp3")
        self.assertEqual(status, 403)

    def test_head_rejects_another_ip(self):
        status, _, _ = request(self.handler, "HEAD", "/cast2nest.py")
        self.assertEqual(status, 403)

    def test_head_rejects_an_unregistered_file(self):
        status, _, _ = request(self.handler, "HEAD", "/cast2nest.py", CAST_IP)
        self.assertEqual(status, 404)

    def test_head_returns_get_headers_without_a_body(self):
        status, headers, body = request(
            self.handler, "HEAD", "/media/audio.mp3", CAST_IP
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-length"], "10")
        self.assertEqual(body, b"")

    def test_missing_device_ip_fails_closed(self):
        self.handler.allowed_ip = None
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                status, _, _ = request(
                    self.handler, method, "/media/audio.mp3", CAST_IP
                )
                self.assertEqual(status, 403)

    def test_valid_range_returns_only_requested_bytes(self):
        status, headers, body = request(
            self.handler,
            "GET",
            "/media/audio.mp3",
            CAST_IP,
            {"Range": "bytes=2-5"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["content-range"], "bytes 2-5/10")
        self.assertEqual(body, b"2345")

    def test_head_applies_the_same_range_validation(self):
        status, headers, body = request(
            self.handler,
            "HEAD",
            "/media/audio.mp3",
            CAST_IP,
            {"Range": "bytes=2-5"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["content-length"], "4")
        self.assertEqual(body, b"")

    def test_unsatisfiable_ranges_return_416(self):
        for value in ("bytes=999999999-", "bytes=-0", "bytes=5-3"):
            with self.subTest(value=value):
                status, headers, _ = request(
                    self.handler,
                    "GET",
                    "/media/audio.mp3",
                    CAST_IP,
                    {"Range": value},
                )
                self.assertEqual(status, 416)
                self.assertEqual(headers["content-range"], "bytes */10")

    def test_malformed_range_is_ignored(self):
        status, _, body = request(
            self.handler,
            "GET",
            "/media/audio.mp3",
            CAST_IP,
            {"Range": "bytes=unknown"},
        )
        self.assertEqual((status, body), (200, b"0123456789"))

    def test_empty_file_has_zero_content_length(self):
        self.file_path.write_bytes(b"")
        status, headers, body = request(
            self.handler, "GET", "/media/audio.mp3", CAST_IP
        )
        self.assertEqual((status, headers["content-length"], body), (200, "0", b""))

    def test_unresolved_cast_ip_aborts_before_binding(self):
        with (
            patch.object(cast2nest, "cast_ip_of", return_value=None),
            patch.object(cast2nest, "_serve_on") as serve,
            self.assertRaises(ConnectionError),
        ):
            cast2nest.serve_files(8099, "unresolved.invalid")
        serve.assert_not_called()


class StdinHandlerTests(unittest.TestCase):
    def setUp(self):
        def capture_server(handler_cls, port):
            return types.SimpleNamespace(handler_cls=handler_cls)

        with patch.object(cast2nest, "_serve_on", side_effect=capture_server):
            self.server = cast2nest.serve_stdin(cast2nest.LiveBuffer(), 8099, CAST_IP)
        self.handler = self.server.handler_cls

    def test_head_rejects_another_ip(self):
        status, _, _ = request(self.handler, "HEAD", "/cast2nest.py")
        self.assertEqual(status, 403)

    def test_head_rejects_an_unregistered_route(self):
        status, _, _ = request(self.handler, "HEAD", "/stream.mp3", CAST_IP)
        self.assertEqual(status, 404)

    def test_head_accepts_only_the_random_stream_route(self):
        status, headers, body = request(
            self.handler, "HEAD", self.server.route, CAST_IP
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "audio/mpeg")
        self.assertEqual(body, b"")


class PlaybackTests(unittest.TestCase):
    def test_buffering_then_error_is_a_failure(self):
        result = monitor([media_status("BUFFERING"), media_status("IDLE", "ERROR")])
        self.assertFalse(result)

    def test_playing_then_finished_is_a_success(self):
        result = monitor([media_status("PLAYING"), media_status("IDLE", "FINISHED")])
        self.assertTrue(result)

    def test_load_failed_stops_monitoring(self):
        result = monitor([{"ns": cast2nest.NS_MEDIA, "json": {"type": "LOAD_FAILED"}}])
        self.assertFalse(result)

    def test_stdin_monitor_failure_returns_an_error(self):
        buffer = cast2nest.LiveBuffer()
        buffer.push(b"audio")
        process = Mock()
        process.poll.return_value = None
        server = Mock()
        server.listen_port = 8099
        server.route = "/random/stream.mp3"
        session = Mock()
        session.monitor.return_value = False
        app = {"transportId": "transport"}

        with (
            patch.object(cast2nest.shutil, "which", return_value="/usr/bin/ffmpeg"),
            patch.object(cast2nest, "require_cast_ip", return_value=CAST_IP),
            patch.object(cast2nest, "start_stdin_ffmpeg", return_value=process),
            patch.object(cast2nest, "serve_stdin", return_value=server),
            patch.object(cast2nest, "CastSession", return_value=session),
            patch.object(cast2nest, "ensure_app", return_value=app),
            patch.object(cast2nest, "local_ip", return_value="192.0.2.2"),
            patch.object(cast2nest.time, "sleep"),
            patch.object(cast2nest.sys.stdin, "isatty", return_value=True),
            self.assertRaises(SystemExit) as raised,
        ):
            cast2nest.cmd_play_stdin("cast.local", 8009, {}, 8099)
        self.assertNotIn(raised.exception.code, (None, 0))


if __name__ == "__main__":
    unittest.main()
