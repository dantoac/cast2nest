#!/usr/bin/env python3
# Copyright (C) 2026 Daniel Aguayo
# SPDX-License-Identifier: GPL-3.0-or-later
# -*- coding: utf-8 -*-
"""
cast2nest.py -- Sends audio to a Google Cast device (Nest Mini, Chromecast, etc.)
using ONLY the Python stdlib: no pip, no pychromecast.

CastV2 protocol implemented by hand:
  - frames: big-endian uint32 length + protobuf CastMessage (fields 1..7)
  - namespaces: connection / heartbeat / deviceauth / receiver / media
  - auth: empty AuthChallenge (as VLC does) -- if the firmware rejects it,
    retry without the authentication handshake
  - the device downloads audio over HTTP from this machine (http.server
    with Content-Length + Range, for retries and recovery from interruptions)

Usage:
  python3 cast2nest.py [--host IP] play TRACKS...       # 1+ file|URL, or an
      # entire directory (filtered for audio), or an .m3u/.m3u8 playlist
  python3 cast2nest.py [--host IP] play - < audio.mp3   # pipe from another app
      # raw audio over a pipe: add --raw (or fine-tune with --in-fmt/--in-rate/--in-ch)
  python3 cast2nest.py [--host IP] volume LEVEL          # 0..100
  python3 cast2nest.py [--host IP] stop
  python3 cast2nest.py [--host IP] status

Without --host, discover the Cast device automatically by scanning port 8009 on
the /24 subnet.

Note: during playback, this process must remain running to serve tracks over
HTTP. On Ctrl-C, the current track continues on the device until it finishes or
you use `stop`; at the end of the playlist, the app closes automatically.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

SRC_ID = "sender-0"
NS_CONN = "urn:x-cast:com.google.cast.tp.connection"
NS_HB = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_AUTH = "urn:x-cast:com.google.cast.tp.deviceauth"
NS_RECV = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
APP_DEFAULT = "CC1AD845"  # Default Media Receiver
# Only formats that ALL Cast devices can reliably decode are copied unchanged;
# all others (ogg, m4a, opus, etc.) are converted ONCE to mp3 with ffmpeg and
# cached in CACHE_DIR: they are then served with Range/Length like a native mp3
NATIVE = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav"}
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "cast2nest")
# extensions to discover when expanding directories
AUDIO_EXT = {
    ".mp3",
    ".flac",
    ".wav",
    ".ogg",
    ".oga",
    ".opus",
    ".m4a",
    ".aac",
    ".mka",
    ".wma",
    ".aif",
    ".aiff",
}


# ──────────────── protobuf CastMessage (manual construction/parsing) ────────────────


def enc_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def dec_varint(buf, i):
    res = sh = 0
    while True:
        b = buf[i]
        i += 1
        res |= (b & 0x7F) << sh
        if not b & 0x80:
            return res, i
        sh += 7


def build_msg(src, dst, ns, utf8=None, binary=None):
    """CastMessage: 1=protocol_version(varint) 2=source_id 3=dest_id 4=namespace
    5=payload_type(varint) 6=payload_utf8 7=payload_binary"""
    m = bytearray(b"\x08\x00")  # protocol_version = CASTV2_1_0
    for fn, val in ((2, src), (3, dst), (4, ns)):
        v = val.encode()
        m += enc_varint((fn << 3) | 2) + enc_varint(len(v)) + v
    if binary is not None:
        m += b"\x28\x01"  # payload_type = BINARY
        m += enc_varint((7 << 3) | 2) + enc_varint(len(binary)) + binary
    else:
        v = (utf8 or "").encode()
        m += b"\x28\x00"  # payload_type = STRING
        m += enc_varint((6 << 3) | 2) + enc_varint(len(v)) + v
    return bytes(m)


def parse_msg(buf):
    f = {}
    i = 0
    while i < len(buf):
        tag, i = dec_varint(buf, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            f[fn], i = dec_varint(buf, i)
        elif wt == 2:
            ln, i = dec_varint(buf, i)
            f[fn] = buf[i : i + ln]
            i += ln
        elif wt == 5:
            f[fn] = buf[i : i + 4]
            i += 4
        elif wt == 1:
            f[fn] = buf[i : i + 8]
            i += 8
        else:
            raise ValueError(f"unexpected wire type {wt}")
    return {
        "src": f.get(2, b"").decode(errors="replace"),
        "dst": f.get(3, b"").decode(errors="replace"),
        "ns": f.get(4, b"").decode(errors="replace"),
        "json": json.loads(f[6]) if 6 in f else None,
        "bin": f.get(7),
    }


# ────────────────────────── Cast session ──────────────────────────


class CastSession:
    def __init__(self, host, port=8009, timeout=8.0):
        self.host, self.port, self.timeout, self.req = host, port, timeout, 0
        try:
            self._open(do_auth=True)
        except (ConnectionError, ssl.SSLError, OSError):
            # firmware is picky about the handshake: reconnect without auth
            self._open(do_auth=False)

    def _open(self, do_auth):
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(raw)
        self.send(
            NS_CONN,
            "receiver-0",
            {"type": "CONNECT", "userAgent": "cast2nest.py", "ver": "2.0"},
        )
        if do_auth:
            # empty AuthChallenge (no Google certificate, like VLC)
            self.send_bin(NS_AUTH, "receiver-0", b"\x0a\x00")
            m = self.wait(lambda m: m["ns"] == NS_AUTH, timeout=4, hb=False)
            if m is not None and m["bin"] is not None:
                pass  # AuthResponse/AuthError received; continue either way
        self._do_auth = do_auth

    def send(self, ns, dst, obj):
        self._raw(
            build_msg(SRC_ID, dst, ns, utf8=json.dumps(obj, separators=(",", ":")))
        )

    def send_bin(self, ns, dst, data):
        self._raw(build_msg(SRC_ID, dst, ns, binary=data))

    def _raw(self, frame):
        self.sock.sendall(struct.pack(">I", len(frame)) + frame)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("the device closed the connection")
            buf += chunk
        return buf

    def recv_msg(self, timeout=8):
        self.sock.settimeout(timeout)
        ln = struct.unpack(">I", self._recv_exact(4))[0]
        return parse_msg(self._recv_exact(ln))

    def wait(self, pred, timeout=10, hb=True):
        t0, last_ping = time.time(), 0.0
        while True:
            left = timeout - (time.time() - t0)
            if left <= 0:
                return None
            if select.select([self.sock], [], [], min(0.25, max(0.01, left)))[0]:
                m = self.recv_msg(timeout=max(0.2, left))
                if m["ns"] == NS_HB and (m["json"] or {}).get("type") == "PING":
                    self.send(NS_HB, m["src"], {"type": "PONG"})
                    continue
                if pred(m):
                    return m
            if hb and time.time() - last_ping > 4:
                self.send(NS_HB, "receiver-0", {"type": "PING"})
                last_ping = time.time()

    def get_status(self):
        self.req += 1
        rid = self.req
        self.send(NS_RECV, "receiver-0", {"type": "GET_STATUS", "requestId": rid})
        m = self.wait(
            lambda m: (
                m["ns"] == NS_RECV
                and (m["json"] or {}).get("type") == "RECEIVER_STATUS"
                and m["json"].get("requestId") in (None, rid)
            ),
            timeout=8,
        )
        if not m:
            raise RuntimeError("did not receive RECEIVER_STATUS from the device")
        return m["json"]["status"]

    def launch(self, app_id):
        self.req += 1
        self.send(
            NS_RECV,
            "receiver-0",
            {"type": "LAUNCH", "appId": app_id, "requestId": self.req},
        )
        m = self.wait(
            lambda m: (
                m["ns"] == NS_RECV
                and (m["json"] or {}).get("type") == "RECEIVER_STATUS"
                and m["json"].get("status", {}).get("applications")
            ),
            timeout=12,
        )
        if not m:
            raise RuntimeError("the device did not launch the playback app")
        app = next(
            (
                a
                for a in m["json"]["status"]["applications"]
                if a.get("appId") == app_id
            ),
            None,
        )
        if not app:
            raise RuntimeError(f"app {app_id} did not become active")
        return app

    def connect_v2(self, transport_id):
        self.send(NS_CONN, transport_id, {"type": "CONNECT", "origin": {}})

    def load(self, transport_id, url, ctype, title):
        self.req += 1
        self.send(
            NS_MEDIA,
            transport_id,
            {
                "type": "LOAD",
                "requestId": self.req,
                "media": {
                    "contentId": url,
                    "streamType": "BUFFERED",
                    "contentType": ctype,
                    "metadata": {
                        "metadataType": 3,
                        "title": title,
                        "artist": "cast2nest.py",
                    },
                },
                "autoplay": True,
                "currentTime": 0,
                "customData": {},
            },
        )

    def monitor(self):
        """Return True only if the track reached PLAYING and finished without error."""
        played, last, last_ping, ok = False, None, 0.0, False
        while True:
            now = time.time()
            if now - last_ping > 4:
                try:
                    self.send(NS_HB, "receiver-0", {"type": "PING"})
                except (ConnectionError, ssl.SSLError, OSError):
                    break
                last_ping = now
            if select.select([self.sock], [], [], 1.0)[0]:
                try:
                    m = self.recv_msg(timeout=2)
                except (ConnectionError, ssl.SSLError, OSError):
                    if not played:
                        print(
                            "the device closed the connection before playback "
                            "started (did it reject the file?)"
                        )
                    ok = played
                    break
                if m["ns"] == NS_HB and (m["json"] or {}).get("type") == "PING":
                    self.send(NS_HB, m["src"], {"type": "PONG"})
                    continue
                if (
                    m["ns"] == NS_RECV
                    and (m["json"] or {}).get("type") == "RECEIVER_STATUS"
                    and not any(
                        a.get("appId") == APP_DEFAULT
                        for a in m["json"]["status"].get("applications", [])
                    )
                ):
                    print(
                        "the playback app closed"
                        + ("" if played else " before playback started")
                    )
                    ok = played
                    break
                if m["ns"] != NS_MEDIA:
                    continue
                payload = m["json"] or {}
                msg_type = payload.get("type")
                if msg_type in ("LOAD_FAILED", "LOAD_CANCELLED", "ERROR"):
                    print(f"error: the device rejected the track ({msg_type})")
                    break
                if msg_type != "MEDIA_STATUS":
                    continue
                s0 = (payload.get("status") or [{}])[0]
                ps = s0.get("playerState") or "IDLE"
                if ps != last:
                    print(f"state: {ps}")
                    last = ps
                if ps == "PLAYING":
                    played = True
                if ps == "IDLE":
                    if s0.get("idleReason") == "ERROR":
                        print(
                            "error: the device could not decode the file "
                            "(unsupported or corrupt format?)"
                        )
                        break
                    if played:
                        print("playback finished")
                        ok = True
                        break
        return ok


# ────────────────────────── discovery and networking ──────────────────────────


def discover(timeout=0.8):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 53))
    base = s.getsockname()[0].rsplit(".", 1)[0]
    s.close()
    found, lock = [], threading.Lock()

    def probe(ip):
        c = socket.socket()
        c.settimeout(timeout)
        ok = c.connect_ex((ip, 8009)) == 0
        c.close()
        if ok:
            with lock:
                found.append(ip)

    ths = [
        threading.Thread(target=probe, args=(f"{base}.{i}",), daemon=True)
        for i in range(1, 255)
    ]
    for i in range(0, len(ths), 64):
        for t in ths[i : i + 64]:
            t.start()
        for t in ths[i : i + 64]:
            t.join()
    return found


def local_ip(dst):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dst, 53))
        return s.getsockname()[0]
    finally:
        s.close()


def pick_host():
    found = discover()
    if not found:
        sys.exit("error: no Cast device found on the subnet (use --host IP)")
    if len(found) > 1:
        print(
            "note: multiple Cast devices found:", ", ".join(found), "; using the first"
        )
    return found[0]


# ────────────────────────── media preparation ──────────────────────────


def is_url(s):
    return s.startswith(("http://", "https://"))


def has_encoder(name):
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    return name in out


def media_title(path, preset=None):
    """Track title: preset -> 'title' tag (ffprobe) -> filename."""
    if preset:
        return preset
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format_tags=title:stream_tags=title",
                "-of",
                "default=nw=1:nk=1",
                path,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        for ln in r.stdout.splitlines():
            if ln.strip():
                return ln.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.splitext(os.path.basename(path))[0]


def expand_media_args(args_list):
    """Resolve play arguments -> [{media, title}, ...].
    Accept files, URLs, directories (expanded in order and filtered for audio),
    and .m3u/.m3u8 playlists. A nonexistent argument after a track is treated
    as its title (compatible with 'play file.mp3 "My title"')."""
    out = []
    for a in args_list:
        if a == "-":  # pipe from another app: single track
            out.append({"media": "-", "title": None})
            continue
        if is_url(a):
            out.append({"media": a, "title": None})
            continue
        p = os.path.abspath(os.path.expanduser(a))
        if os.path.isdir(p):
            for fn in sorted(os.listdir(p), key=str.lower):
                if os.path.splitext(fn)[1].lower() in AUDIO_EXT:
                    out.append({"media": os.path.join(p, fn), "title": None})
            continue
        if os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in (".m3u", ".m3u8"):
                d = os.path.dirname(p)
                with open(p, encoding="utf-8", errors="replace") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln or ln.startswith("#"):
                            continue
                        if not is_url(ln):
                            ln = os.path.normpath(os.path.join(d, ln))
                        out.append({"media": ln, "title": None})
            else:
                out.append({"media": p, "title": None})
            continue
        if out:  # does not exist: use it as the previous track's title
            out[-1]["title"] = a
        else:
            sys.exit(f"error: does not exist: {a}")
    return out


def ffmpeg_convert(src, dst, enc, out_fmt=None):
    """Run ffmpeg with progress on a self-updating line. Return (rc, error)."""
    total = None
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            src,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    try:
        total = float(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        pass
    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-i",
            src,
            "-vn",
            *enc,
            *(["-f", out_fmt] if out_fmt else []),
            dst,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    err = []
    for line in proc.stdout:
        line = line.strip()
        k, _, v = line.partition("=")
        if k in ("out_time_us", "out_time_ms"):
            try:  # these fields are in microseconds
                pct = f"{min(99, int(int(v) / 1e6 * 100 / total))}% " if total else ""
            except ValueError:
                pct = ""
            print(f"\rconverting... {pct}        ", end="", flush=True)
        elif (
            k
            not in (
                "frame",
                "fps",
                "bitrate",
                "total_size",
                "progress",
                "speed",
                "drop_frames",
                "multiframe",
            )
            and line
        ):
            err.append(line)
    proc.wait()
    print("\r" + " " * 32 + "\r", end="", flush=True)
    return proc.returncode, ("; ".join(err[-2:]) if err else "")


def prepare(media, preset_title=None):
    """-> (kind, payload, contentType, title); kind: 'url' | 'file'"""
    if is_url(media):
        path = urllib.parse.urlparse(media).path
        title = preset_title or os.path.splitext(os.path.basename(path))[0] or "stream"
        return "url", media, "audio/mpeg", title
    src = os.path.abspath(media)
    if not os.path.isfile(src):
        sys.exit(f"error: file does not exist: {src}")
    ext = os.path.splitext(src)[1].lower()
    stem = os.path.splitext(os.path.basename(src))[0]
    title = media_title(src, preset_title)
    if ext in NATIVE:  # the Cast device decodes it natively: serve it unchanged
        return "file", (os.path.dirname(src), os.path.basename(src)), NATIVE[ext], title
    if not shutil.which("ffmpeg"):
        sys.exit("error: unsupported format and ffmpeg is not installed")
    lame = has_encoder("libmp3lame")
    exts, ctype = (".mp3", "audio/mpeg") if lame else (".m4a", "audio/mp4")
    enc = (
        ["-c:a", "libmp3lame", "-b:a", "192k"]
        if lame
        else ["-c:a", "aac", "-b:a", "192k"]
    )
    key = hashlib.sha1(
        f"{src}|{os.path.getsize(src)}|{os.path.getmtime(src):.6f}".encode()
    ).hexdigest()[:16]
    d = CACHE_DIR
    os.makedirs(d, exist_ok=True)
    cached = os.path.join(d, f"{stem[:60]}.{key}{exts}")
    if not os.path.isfile(cached):
        tmp = cached + ".tmp"
        rc, tail = ffmpeg_convert(src, tmp, enc, "mp3" if lame else "ipod")
        if rc != 0:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            sys.exit(f"error: ffmpeg conversion failed:\n{tail}")
        os.replace(tmp, cached)
    return "file", (d, os.path.basename(cached)), ctype, title


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # to ~/.cache/cast2nest/serve.log (diagnostics)
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(os.path.join(CACHE_DIR, "serve.log"), "ab") as f:
                f.write(
                    f"{time.strftime('%d/%m %H:%M:%S')} {self.address_string()} "
                    f"{fmt % args}\n".encode()
                )
        except OSError:
            pass

    def log_request(self, code="-", size="-"):
        self.log_message(
            '"%s" %s range=%s',
            self.requestline,
            str(code),
            self.headers.get("Range") or "-",
        )


class UnsatisfiableRange(ValueError):
    """The requested range has no available bytes."""


class RangeFileHandler(QuietHandler):
    """Serve registered files with Range support without exposing other files.

    GET and HEAD share IP and path validation. The HEAD response returns the
    same headers as GET but never opens or sends the file.
    """

    CHUNK = 1 << 20
    routes: ClassVar[dict[str, str]] = {}
    allowed_ip = None

    def do_GET(self):
        self._serve_file()

    def do_HEAD(self):
        self._serve_file()

    def _requested_file(self):
        if self.allowed_ip is None or self.client_address[0] != self.allowed_ip:
            self.send_error(403)
            return None
        fpath = self.routes.get(urllib.parse.urlsplit(self.path).path)
        if not fpath or not os.path.isfile(fpath):
            self.send_error(404)
            return None
        return fpath

    @staticmethod
    def _range_bounds(header, size):
        """Return (start, end), or None if there is no valid single range."""
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip()) if header else None
        if not m or not (m.group(1) or m.group(2)):
            return None
        first, last = m.groups()
        if first:
            start = int(first)
            if start >= size:
                raise UnsatisfiableRange
            end = min(size - 1, int(last)) if last else size - 1
            if end < start:
                raise UnsatisfiableRange
        else:
            suffix_length = int(last)
            if suffix_length == 0 or size == 0:
                raise UnsatisfiableRange
            start = max(0, size - suffix_length)
            end = size - 1
        return start, end

    def _serve_file(self):
        fpath = self._requested_file()
        if fpath is None:
            return
        size = os.path.getsize(fpath)
        try:
            bounds = self._range_bounds(self.headers.get("Range", ""), size)
        except UnsatisfiableRange:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            self.close_connection = True
            return

        partial = bounds is not None
        start, end = bounds if partial else (0, size - 1)
        try:
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", self.guess_type(fpath) or "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "close")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with open(fpath, "rb") as f:
                    f.seek(start)
                    rest = max(0, end - start + 1)
                    while rest > 0:
                        b = f.read(min(self.CHUNK, rest))
                        if not b:
                            break
                        self.wfile.write(b)
                        rest -= len(b)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the Cast device stops downloading when its buffer is full: normal
        finally:
            self.close_connection = True


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # the Cast device aborts the download when finished or stopped: not a real error
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


def _serve_on(handler, port):
    """Bind the server; if the port is busy, fall back to a free ephemeral port."""
    try:
        srv = QuietServer(("0.0.0.0", port), handler)
    except OSError as e:
        if e.errno not in (48, 98):  # EADDRINUSE (macOS / Linux)
            raise
        print(f"warning: port {port} is busy; using a free port")
        srv = QuietServer(("0.0.0.0", 0), handler)
    srv.listen_port = srv.server_address[1]
    return _run(srv)


def cast_ip_of(cast_host):
    try:
        return socket.gethostbyname(cast_host)
    except OSError:
        return None


def require_cast_ip(cast_host):
    """Resolve the device or abort; never disable the IP filter."""
    cast_ip = cast_ip_of(cast_host)
    if cast_ip is None:
        raise ConnectionError(f"could not resolve the Cast device IP: {cast_host}")
    return cast_ip


def serve_files(port, cast_host):
    """Serve individual files; register each one with `publish`."""
    cast_ip = require_cast_ip(cast_host)
    handler = type("Files", (RangeFileHandler,), {"routes": {}, "allowed_ip": cast_ip})
    srv = _serve_on(handler, port)
    srv.routes = handler.routes
    return srv


def publish(srv, path):
    """Register `path` at an unpredictable route and return that URL route."""
    route = f"/{secrets.token_urlsafe(16)}/{urllib.parse.quote(os.path.basename(path))}"
    srv.routes[route] = path
    return route


def _run(srv):
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class LiveBuffer:
    """Sequence-numbered chunks: one producer, N live consumers.

    Each consumer records its next sequence. Only chunks read by every active
    consumer are pruned; with no consumers, the capacity limit applies while
    data accumulates before the first connection.
    """

    def __init__(self, cap=16 << 20):
        self.lock = threading.Condition()
        self.slots, self.seq, self.size = [], 0, 0
        self.cap, self.eof = cap, False
        self.consumers = {}
        self.next_consumer_id = 0

    def push(self, b):
        with self.lock:
            self.slots.append((self.seq, b))
            self.seq += 1
            self.size += len(b)
            self.lock.notify_all()
            while self.size > self.cap and self.consumers:
                self.lock.wait(0.2)
            while self.size > self.cap and len(self.slots) > 1:
                _, old = self.slots.pop(0)
                self.size -= len(old)

    def end(self):
        with self.lock:
            self.eof = True
            self.lock.notify_all()

    def add_consumer(self, from_live=False):
        """Register a consumer and return its identifier."""
        with self.lock:
            self.next_consumer_id += 1
            consumer_id = self.next_consumer_id
            self.consumers[consumer_id] = (
                self.seq
                if from_live
                else (self.slots[0][0] if self.slots else self.seq)
            )
            return consumer_id

    def drop_consumer(self, consumer_id):
        with self.lock:
            self.consumers.pop(consumer_id, None)
            self._prune_consumed()
            self.lock.notify_all()

    def _prune_consumed(self):
        """Remove chunks consumed by all; the lock is already held."""
        if not self.consumers:
            self.slots.clear()
            self.size = 0
            return
        oldest_needed = min(self.consumers.values())
        while self.slots and self.slots[0][0] < oldest_needed:
            _, old = self.slots.pop(0)
            self.size -= len(old)

    def stream(self, consumer_id):
        """Generate chunks for a registered consumer."""
        while True:
            got = None
            with self.lock:
                while True:
                    nxt = self.consumers.get(consumer_id)
                    if nxt is None:
                        return
                    for s, b in self.slots:
                        if s >= nxt:
                            nxt, got = s, b
                            break
                    if got is not None or self.eof:
                        break
                    self.lock.wait(0.5)
                if got is None:  # EOF with no more data for this consumer
                    return
                self.consumers[consumer_id] = nxt + 1
                self._prune_consumed()
                self.lock.notify_all()
            yield got


def start_stdin_ffmpeg(flags, buf):
    """ffmpeg reads our stdin -> mp3 -> buffer; a feeder thread pushes it."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if flags.get("fmt"):
        cmd += ["-f", flags["fmt"]]
        if flags.get("rate"):
            cmd += ["-ar", str(flags["rate"])]
        if flags.get("ch"):
            cmd += ["-ac", str(flags["ch"])]
    cmd += [
        "-i",
        "pipe:0",
        "-vn",
        "-f",
        "mp3",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def feeder():
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                buf.push(chunk)
        except (OSError, ValueError):
            pass
        finally:
            buf.end()

    threading.Thread(target=feeder, daemon=True).start()
    return proc


def serve_stdin(buf, port, cast_ip):
    """Live stream on a random route, restricted to the Cast device IP."""
    n_get = [0]
    route = f"/{secrets.token_urlsafe(16)}/stream.mp3"

    class StdinHandler(QuietHandler):
        def _authorized(self):
            if self.client_address[0] != cast_ip:
                self.send_error(403)
                return False
            if urllib.parse.urlsplit(self.path).path != route:
                self.send_error(404)
                return False
            return True

        def do_HEAD(self):
            if not self._authorized():
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def do_GET(self):
            if not self._authorized():
                return
            n_get[0] += 1
            consumer_id = buf.add_consumer(from_live=n_get[0] > 1)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in buf.stream(consumer_id):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                buf.drop_consumer(consumer_id)

    srv = _serve_on(StdinHandler, port)
    srv.route = route
    return srv


# ────────────────────────── commands ──────────────────────────


def ensure_app(sess):
    """Return the active playback app after connecting v2 to its session."""
    st = sess.get_status()
    app = next(
        (a for a in st.get("applications", []) if a.get("appId") == APP_DEFAULT), None
    )
    app = app or sess.launch(APP_DEFAULT)
    sess.connect_v2(app["transportId"])
    return app


def cmd_play(host, port_cast, media_args, port_http, stdin_flags=None):
    tracks = expand_media_args(media_args)
    if any(t["media"] == "-" for t in tracks):
        if len(tracks) > 1:
            print(
                "note: '-' (standard input) must be the only track; ignoring the rest"
            )
        return cmd_play_stdin(host, port_cast, stdin_flags or {}, port_http)
    total = len(tracks)
    if not total:
        sys.exit("error: no tracks to play")
    print(f"playlist: {total} track{'s' if total > 1 else ''}")
    srv = None
    ok_tracks = 0
    try:
        sess = CastSession(host, port_cast)
        app = ensure_app(sess)
        print(f"active app: {app.get('displayName', 'Default Media Receiver')}")
        for i, tr in enumerate(tracks, 1):
            media = tr["media"]
            try:
                kind, payload, ctype, title = prepare(media, tr["title"])
            except SystemExit:
                print(f"note: track [{i}/{total}] is inaccessible: {media}")
                continue
            if kind == "url":
                url = payload
            elif kind == "file":
                d, fn = payload
                if srv is None:
                    srv = serve_files(port_http, host)
                route = publish(srv, os.path.join(d, fn))
                url = f"http://{local_ip(host)}:{srv.listen_port}{route}"
            app = ensure_app(sess)
            print(f"\n[{i}/{total}] {title}")
            print(f"   URL: {url}")
            sess.load(app["transportId"], url, ctype, title)
            if not sess.monitor():
                print(f"note: skipping track [{i}/{total}]")
                continue
            ok_tracks += 1
        if not ok_tracks:
            print("error: no tracks could be played")
            sys.exit(1)
        try:  # end of playlist: close the app and leave the speaker idle
            app = ensure_app(sess)
            sess.req += 1
            sess.send(
                NS_RECV,
                "receiver-0",
                {"type": "STOP", "sessionId": app["sessionId"], "requestId": sess.req},
            )
        except (ConnectionError, ssl.SSLError, OSError, RuntimeError):
            pass
        print(f"playlist finished ({ok_tracks}/{total} tracks played)")
    except KeyboardInterrupt:
        print(
            "\ninterrupted: audio continues on the device until it finishes"
            " (or until someone stops it)"
        )
    finally:
        if srv:
            srv.shutdown()


def cmd_play_stdin(host, port_cast, flags, port_http):
    """play -: stream standard input (live transcode -> mp3)."""
    if not shutil.which("ffmpeg"):
        sys.exit("error: ffmpeg is required to use '-'")
    cast_ip = require_cast_ip(host)
    buf = LiveBuffer()
    proc = start_stdin_ffmpeg(flags, buf)
    srv = serve_stdin(buf, port_http, cast_ip)
    if sys.stdin.isatty():
        print(
            "warning: waiting for audio on stdin... (for raw PCM: "
            "--in-fmt s16le --in-rate 48000 --in-ch 2)"
        )
    else:
        print("note: converting standard input to mp3 live")
    time.sleep(0.5)
    with buf.lock:
        got_any = buf.seq > 0
    if proc.poll() is not None and not got_any:
        # ffmpeg exited without output: unrecognized input
        srv.shutdown()
        sys.exit(
            "error: ffmpeg could not read the input; is the format recognized? "
            "for raw audio: --in-fmt s16le --in-rate 48000 --in-ch 2"
        )
    try:
        sess = CastSession(host, port_cast)
        app = ensure_app(sess)
        print(f"active app: {app.get('displayName', 'Default Media Receiver')}")
        url = f"http://{local_ip(host)}:{srv.listen_port}{srv.route}"
        print(f"standard input\n   URL: {url}")
        sess.load(app["transportId"], url, "audio/mpeg", "standard input")
        if not sess.monitor():
            sys.exit("error: standard input could not be played")
    except KeyboardInterrupt:
        print(
            "\ninterrupted: audio continues on the device until it finishes"
            " (or until someone stops it)"
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
        buf.end()
        srv.shutdown()


def cmd_volume(host, port_cast, level):
    sess = CastSession(host, port_cast)
    if level is None:  # no argument: query only
        v = sess.get_status().get("volume", {})
        print(
            f"volume: {v.get('level', 0) * 100:.0f}%"
            + (" (mute)" if v.get("muted") else "")
        )
        return
    lvl = max(0.0, min(1.0, level / 100.0))
    sess.req += 1
    rid = sess.req
    sess.send(
        NS_RECV,
        "receiver-0",
        {"type": "SET_VOLUME", "volume": {"level": lvl}, "requestId": rid},
    )
    # use the acknowledgment only for synchronization; confirm the level with a
    # fresh GET_STATUS because asynchronous status may contain the previous value
    sess.wait(
        lambda m: (
            m["ns"] == NS_RECV
            and (m["json"] or {}).get("type") in ("SET_VOLUME_REPLY", "RECEIVER_STATUS")
            and m["json"].get("requestId") == rid
        ),
        timeout=4,
    )
    v = sess.get_status().get("volume", {})
    print(
        f"volume: {v.get('level', lvl) * 100:.0f}%"
        + (" (mute)" if v.get("muted") else "")
    )


def cmd_stop(host, port_cast):
    sess = CastSession(host, port_cast)
    apps = sess.get_status().get("applications", [])
    if not apps:
        print("note: nothing is playing")
        return
    for a in apps:
        sess.req += 1
        sess.send(
            NS_RECV,
            "receiver-0",
            {"type": "STOP", "sessionId": a["sessionId"], "requestId": sess.req},
        )
    m = sess.wait(
        lambda m: (
            (m["json"] or {}).get("type") == "RECEIVER_STATUS"
            and not m["json"]["status"].get("applications")
        ),
        timeout=5,
    )
    print("stop" + (" confirmed" if m else " sent"))


def cmd_status(host, port_cast):
    sess = CastSession(host, port_cast)
    st = sess.get_status()
    v = st.get("volume", {})
    print(f"volume: {v.get('level', 0) * 100:.0f}% (mute: {v.get('muted', False)})")
    apps = st.get("applications", [])
    if not apps:
        print("no active apps")
        return
    for a in apps:
        print(
            f"app: {a.get('displayName', a.get('appId'))}  id:{a.get('appId')}"
            f"  session:{a.get('sessionId')}  transport:{a.get('transportId')}"
        )
        if a.get("statusText"):
            print(f"   status: {a['statusText']}")


def main():
    try:  # keep logs readable when output is redirected (nohup, etc.)
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="cast2nest.py",
        description="Send audio to a Google Cast device (Nest Mini, etc.) using only "
        "the Python stdlib (no pip or pychromecast). "
        "Without --host, discover the Cast device by scanning the subnet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # one track (ogg/m4a/etc.: converted ONCE to mp3 in ~/.cache/cast2nest;
  #            subsequent plays start immediately from the cache)
  %(prog)s play ~/Music/sound.mp3

  # manual track title
  %(prog)s play ~/Music/sound.ogg "Code and focus"

  # playlist: a directory, mixed files/URLs, or an .m3u/.m3u8 file
  %(prog)s play ~/Music/caught_in_joy...4h/
  %(prog)s play ~/Music/*.mp3 https://a.radio/stream.mp3
  %(prog)s play ~/Music/coding.m3u

  # pipe from another app (ffmpeg detects mp3/ogg/flac/webm... automatically)
  yt-dlp -o - "URL" | %(prog)s play -
  cat file.ogg | %(prog)s play -

  # RAW audio pipe: --raw (= s16le, 48000 Hz, stereo)
  SINK=$(pactl get-default-sink)
  pactl record SOURCE="${SINK}.monitor" | %(prog)s play - --raw   # all PC audio
  pactl record SOURCE=$(pactl get-default-source) | %(prog)s play - --raw   # microphone
  # capture with other parameters:
  ... | %(prog)s play - --in-fmt f32le --in-rate 44100 --in-ch 2

device control:
  %(prog)s volume        # show current volume (and mute status)
  %(prog)s volume 40     # set volume to 0..100
  %(prog)s stop          # stop playback
  %(prog)s status        # volume + active app
  %(prog)s --host 192.168.1.138 play ...   # skip discovery

behavior:
  - during playback, the process stays alive to serve file(s) over HTTP (:8099)
  - Ctrl-C: the current track continues on the device until it finishes (or `stop`)
  - end of playlist: sends STOP and releases the speaker
  - a track rejected by the firmware is skipped and playback continues
  - HTTP diagnostics (with Range) in ~/.cache/cast2nest/serve.log
""",
    )
    ap.add_argument("--host", help="Cast device IP (otherwise: discover automatically)")
    ap.add_argument("--cast-port", type=int, default=8009)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser(
        "play",
        help="play 1+ file/URL, a directory, "
        "or an .m3u/.m3u8 playlist; '-' pipes stdin",
    )
    p.add_argument("media", nargs="+")
    p.add_argument("--http-port", type=int, default=8099)
    p.add_argument(
        "--raw",
        action="store_true",
        help="'-': raw PCM input, default s16le, 48000 Hz, 2 channels "
        "(adjust manually with --in-rate/--in-ch)",
    )
    p.add_argument("--in-fmt", help="'-': raw input format (e.g. s16le, f32le)")
    p.add_argument("--in-rate", type=int, help="'-': input sample rate (e.g. 44100)")
    p.add_argument("--in-ch", type=int, help="'-': input channel count (e.g. 2)")
    p = sub.add_parser("volume", help="level 0..100; without a value: query only")
    p.add_argument("level", nargs="?", type=float)
    p = sub.add_parser("stop", help="stop playback")
    p = sub.add_parser("status", help="device status")
    args = ap.parse_args()

    host = args.host or pick_host()
    print(f"Cast: {host}:{args.cast_port}")
    if args.cmd == "play":
        fmt = args.in_fmt or ("s16le" if args.raw else None)
        flags = {}
        if fmt:
            flags["fmt"] = fmt
            flags["rate"] = args.in_rate or (48000 if args.raw else None)
            flags["ch"] = args.in_ch or (2 if args.raw else None)
        cmd_play(host, args.cast_port, args.media, args.http_port, flags)
    elif args.cmd == "volume":
        cmd_volume(host, args.cast_port, args.level)
    elif args.cmd == "stop":
        cmd_stop(host, args.cast_port)
    elif args.cmd == "status":
        cmd_status(host, args.cast_port)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
    except ConnectionError as e:
        sys.exit(f"error: connection: {e}")
    except ssl.SSLError as e:
        sys.exit(f"error: TLS: {e}")
