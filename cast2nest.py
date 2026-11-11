#!/usr/bin/env python3
# Copyright (C) 2026 Daniel Aguayo
# SPDX-License-Identifier: GPL-3.0-or-later
# -*- coding: utf-8 -*-
"""
cast2nest.py — Envía audio a un dispositivo Google Cast (Nest Mini, Chromecast, etc.)
usando SOLO la stdlib de Python: sin pip, sin pychromecast.

Protocolo CastV2 implementado a mano:
  · frames: uint32 BE con la longitud + protobuf CastMessage (campos 1..7)
  · namespaces: connection / heartbeat / deviceauth / receiver / media
  · auth: AuthChallenge vacía (como hace VLC) — si el firmware la rechaza,
    reintentamos sin handshake de autenticación
  · el dispositivo descarga el audio por HTTP desde esta máquina (http.server
    con Content-Length + Range, para reintentos/curación de cortes)

Uso:
  python3 cast2nest.py [--host IP] play  PISTAS...      # 1+ archivo|URL, o un
      # directorio entero (filtrado por audio), o una playlist .m3u/.m3u8
  python3 cast2nest.py [--host IP] play - < audio.mp3    # pipa desde otra app
      # audio crudo por pipa: agrega --raw (o fino con --in-fmt/--in-rate/--in-ch)
  python3 cast2nest.py [--host IP] volume NIVEL           # 0..100
  python3 cast2nest.py [--host IP] stop
  python3 cast2nest.py [--host IP] status

Sin --host se descubre el Cast automáticamente escaneando el puerto 8009/24.

Nota: mientras reproduce, este proceso debe seguir vivo (sirve las pistas por
HTTP). Con Ctrl-C la pista actual sigue en el dispositivo hasta terminar o
hasta que uses `stop`; al final de la playlist cortamos la app automaticamente.
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
# solo se copian tal cual los formatos que TODOS los Cast decodifican seguro;
# el resto (ogg, m4a, opus, etc.) se convierte UNA VEZ a mp3 con ffmpeg y queda
# cacheado en CACHE_DIR: se sirve después con Range/Length como un mp3 nativo
NATIVE = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav"}
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "cast2nest")
# extensiones a descubrir al expandir directorios
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


# ────────────────── protobuf CastMessage (armado/parseo manual) ──────────────────


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
            raise ValueError(f"wire type inesperado {wt}")
    return {
        "src": f.get(2, b"").decode(errors="replace"),
        "dst": f.get(3, b"").decode(errors="replace"),
        "ns": f.get(4, b"").decode(errors="replace"),
        "json": json.loads(f[6]) if 6 in f else None,
        "bin": f.get(7),
    }


# ────────────────────────── sesión Cast ──────────────────────────


class CastSession:
    def __init__(self, host, port=8009, timeout=8.0):
        self.host, self.port, self.timeout, self.req = host, port, timeout, 0
        try:
            self._open(do_auth=True)
        except (ConnectionError, ssl.SSLError, OSError):
            # firmware quisquilloso con el handshake: reconectamos sin auth
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
            # AuthChallenge vacía (sin certificado de Google, como VLC)
            self.send_bin(NS_AUTH, "receiver-0", b"\x0a\x00")
            m = self.wait(lambda m: m["ns"] == NS_AUTH, timeout=4, hb=False)
            if m is not None and m["bin"] is not None:
                pass  # AuthResponse/AuthError recibido; seguimos igual
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
                raise ConnectionError("el dispositivo cerró la conexión")
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
            raise RuntimeError("no llegó RECEIVER_STATUS del dispositivo")
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
            raise RuntimeError("el dispositivo no lanzó la app de reproducción")
        app = next(
            (
                a
                for a in m["json"]["status"]["applications"]
                if a.get("appId") == app_id
            ),
            None,
        )
        if not app:
            raise RuntimeError(f"la app {app_id} no quedó activa")
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
        """Devuelve True solo si la pista llegó a PLAYING y terminó sin error."""
        played, last, last_ping, ok = False, None, 0.0, False
        icon = {"PLAYING": "▶", "PAUSED": "⏸", "BUFFERING": "…", "IDLE": "■"}
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
                            "■ el dispositivo cerró la conexión sin llegar a "
                            "sonar (¿rechazó el archivo?)"
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
                        "■ la app de reproducción se cerró"
                        + ("" if played else " sin llegar a sonar")
                    )
                    ok = played
                    break
                if m["ns"] != NS_MEDIA:
                    continue
                payload = m["json"] or {}
                msg_type = payload.get("type")
                if msg_type in ("LOAD_FAILED", "LOAD_CANCELLED", "ERROR"):
                    print(f"✖ el dispositivo rechazó la pista ({msg_type})")
                    break
                if msg_type != "MEDIA_STATUS":
                    continue
                s0 = (payload.get("status") or [{}])[0]
                ps = s0.get("playerState") or "IDLE"
                if ps != last:
                    print(f"   {icon.get(ps, '·')} {ps}")
                    last = ps
                if ps == "PLAYING":
                    played = True
                if ps == "IDLE":
                    if s0.get("idleReason") == "ERROR":
                        print(
                            "✖ el dispositivo no pudo decodificar el "
                            "archivo (¿formato no soportado o corrupto?)"
                        )
                        break
                    if played:
                        print("✔ reproducción terminada")
                        ok = True
                        break
        return ok


# ────────────────────────── descubrimiento y red ──────────────────────────


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
        sys.exit("✖ no se encontró ningún Cast en la subred (usá --host IP)")
    if len(found) > 1:
        print("ℹ varios Cast encontrados:", ", ".join(found), "— usando el primero")
    return found[0]


# ────────────────────────── preparación de medios ──────────────────────────


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
    """Título de una pista: preset → tag 'title' (ffprobe) → nombre del archivo."""
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
    """Resuelve los argumentos de play → [{media, title}, ...].
    Acepta archivos, URLs, directorios (expandidos en orden, filtrando solo
    audio) y playlists .m3u/.m3u8. Un argumento inexistente que sigue a una
    pista se toma como su título (compat con 'play archivo.mp3 "Mi titulo"')."""
    out = []
    for a in args_list:
        if a == "-":  # pipa desde otra app: pista única
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
        if out:  # no existe: es un título para la pista anterior
            out[-1]["title"] = a
        else:
            sys.exit(f"✖ no existe: {a}")
    return out


def ffmpeg_convert(src, dst, enc, out_fmt=None):
    """ffmpeg con progreso (línea que se actualiza). Devuelve (rc, error)."""
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
            try:  # esos campos van en microsegundos
                pct = f"{min(99, int(int(v) / 1e6 * 100 / total))}% " if total else ""
            except ValueError:
                pct = ""
            print(f"\r⟳ convirtiendo… {pct}        ", end="", flush=True)
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
    """→ (tipo, payload, contentType, título); tipo: 'url' | 'file'"""
    if is_url(media):
        path = urllib.parse.urlparse(media).path
        title = preset_title or os.path.splitext(os.path.basename(path))[0] or "stream"
        return "url", media, "audio/mpeg", title
    src = os.path.abspath(media)
    if not os.path.isfile(src):
        sys.exit(f"✖ el archivo no existe: {src}")
    ext = os.path.splitext(src)[1].lower()
    stem = os.path.splitext(os.path.basename(src))[0]
    title = media_title(src, preset_title)
    if ext in NATIVE:  # el Cast lo decodifica nativamente: se sirve tal cual
        return "file", (os.path.dirname(src), os.path.basename(src)), NATIVE[ext], title
    if not shutil.which("ffmpeg"):
        sys.exit("✖ formato no soportado y ffmpeg no está instalado")
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
            sys.exit(f"✖ falló la conversión con ffmpeg:\n{tail}")
        os.replace(tmp, cached)
    return "file", (d, os.path.basename(cached)), ctype, title


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # a ~/.cache/cast2nest/serve.log (diagnóstico)
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
    """El rango solicitado no tiene bytes disponibles."""


class RangeFileHandler(QuietHandler):
    """Sirve archivos registrados con Range, sin exponer otros archivos.

    GET y HEAD comparten la validación de IP y de ruta. La respuesta HEAD
    entrega los mismos headers que GET, pero nunca abre ni envía el archivo.
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
        """Devuelve (inicio, fin), None si no hay un rango simple válido."""
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
                pass  # el Cast corta la descarga al completar su buffer: normal
        finally:
            self.close_connection = True


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # el Cast aborta la descarga al terminar/cortar: no es un error real
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


def _serve_on(handler, port):
    """Ata el servidor; si el puerto está ocupado cae a uno libre (efímero)."""
    try:
        srv = QuietServer(("0.0.0.0", port), handler)
    except OSError as e:
        if e.errno not in (48, 98):  # EADDRINUSE (macOS / Linux)
            raise
        print(f"⚠ puerto {port} ocupado; usando uno libre")
        srv = QuietServer(("0.0.0.0", 0), handler)
    srv.listen_port = srv.server_address[1]
    return _run(srv)


def cast_ip_of(cast_host):
    try:
        return socket.gethostbyname(cast_host)
    except OSError:
        return None


def require_cast_ip(cast_host):
    """Resuelve el dispositivo o aborta; nunca desactiva el filtro de IP."""
    cast_ip = cast_ip_of(cast_host)
    if cast_ip is None:
        raise ConnectionError(f"no se pudo resolver la IP del Cast: {cast_host}")
    return cast_ip


def serve_files(port, cast_host):
    """Servidor de archivos sueltos; registrar cada uno con `publish`."""
    cast_ip = require_cast_ip(cast_host)
    handler = type("Files", (RangeFileHandler,), {"routes": {}, "allowed_ip": cast_ip})
    srv = _serve_on(handler, port)
    srv.routes = handler.routes
    return srv


def publish(srv, path):
    """Registra `path` bajo una ruta impredecible y devuelve esa ruta URL."""
    route = f"/{secrets.token_urlsafe(16)}/{urllib.parse.quote(os.path.basename(path))}"
    srv.routes[route] = path
    return route


def _run(srv):
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class LiveBuffer:
    """Chunks con número de secuencia: 1 productor, N consumidores en vivo.

    Cada consumidor registra su próxima secuencia. Solo se podan los chunks
    que todos los consumidores activos ya leyeron; sin consumidores se aplica
    el límite de capacidad para acumular datos antes de la primera conexión.
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
        """Registra un consumidor y devuelve su identificador."""
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
        """Quita chunks superados por todos; el lock ya está adquirido."""
        if not self.consumers:
            self.slots.clear()
            self.size = 0
            return
        oldest_needed = min(self.consumers.values())
        while self.slots and self.slots[0][0] < oldest_needed:
            _, old = self.slots.pop(0)
            self.size -= len(old)

    def stream(self, consumer_id):
        """Generador de chunks para un consumidor registrado."""
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
                if got is None:  # eof sin más datos para este consumidor
                    return
                self.consumers[consumer_id] = nxt + 1
                self._prune_consumed()
                self.lock.notify_all()
            yield got


def start_stdin_ffmpeg(flags, buf):
    """ffmpeg lee nuestra stdin → mp3 → buffer; un hilo feeder lo empuja."""
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
    """Stream en vivo bajo una ruta aleatoria y solo para la IP del Cast."""
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


# ────────────────────────── comandos ──────────────────────────


def ensure_app(sess):
    """App de reproducción activa + conexión v2 a su sesión; devuelve la app."""
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
                "ℹ '-' (entrada estándar) va solo como pista única; el resto se ignora"
            )
        return cmd_play_stdin(host, port_cast, stdin_flags or {}, port_http)
    total = len(tracks)
    if not total:
        sys.exit("✖ no hay pistas para reproducir")
    print(f"▶ playlist: {total} pista{'s' if total > 1 else ''}")
    srv = None
    ok_tracks = 0
    try:
        sess = CastSession(host, port_cast)
        app = ensure_app(sess)
        print(f"✔ {app.get('displayName', 'Default Media Receiver')} activa")
        for i, tr in enumerate(tracks, 1):
            media = tr["media"]
            try:
                kind, payload, ctype, title = prepare(media, tr["title"])
            except SystemExit:
                print(f"ℹ pista [{i}/{total}] no accesible: {media}")
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
            print(f"\n▶ [{i}/{total}] {title}")
            print(f"   URL: {url}")
            sess.load(app["transportId"], url, ctype, title)
            if not sess.monitor():
                print(f"ℹ salteando la pista [{i}/{total}]")
                continue
            ok_tracks += 1
        if not ok_tracks:
            print("✖ ninguna pista pudo reproducirse")
            sys.exit(1)
        try:  # fin de playlist: cerramos la app y dejamos el altavoz limpio
            app = ensure_app(sess)
            sess.req += 1
            sess.send(
                NS_RECV,
                "receiver-0",
                {"type": "STOP", "sessionId": app["sessionId"], "requestId": sess.req},
            )
        except (ConnectionError, ssl.SSLError, OSError, RuntimeError):
            pass
        print(f"🏁 playlist terminada ({ok_tracks}/{total} pistas sonaron)")
    except KeyboardInterrupt:
        print(
            "\n⏱ interrumpido: el audio sigue en el dispositivo hasta terminar"
            " (o hasta que alguien lo corte)"
        )
    finally:
        if srv:
            srv.shutdown()


def cmd_play_stdin(host, port_cast, flags, port_http):
    """play -: transmite lo que entra por stdin (transcode en vivo → mp3)."""
    if not shutil.which("ffmpeg"):
        sys.exit("✖ para usar '-' se necesita ffmpeg")
    cast_ip = require_cast_ip(host)
    buf = LiveBuffer()
    proc = start_stdin_ffmpeg(flags, buf)
    srv = serve_stdin(buf, port_http, cast_ip)
    if sys.stdin.isatty():
        print(
            "⚠ esperando audio por stdin… (si es PCM crudo: "
            "--in-fmt s16le --in-rate 48000 --in-ch 2)"
        )
    else:
        print("ℹ leyendo entrada estándar → ffmpeg → mp3 en vivo")
    time.sleep(0.5)
    with buf.lock:
        got_any = buf.seq > 0
    if proc.poll() is not None and not got_any:
        # ffmpeg salió sin producir nada: entrada no reconocida
        srv.shutdown()
        sys.exit(
            "✖ ffmpeg no pudo leer la entrada; ¿formato reconocible? "
            "para audio crudo: --in-fmt s16le --in-rate 48000 --in-ch 2"
        )
    try:
        sess = CastSession(host, port_cast)
        app = ensure_app(sess)
        print(f"✔ {app.get('displayName', 'Default Media Receiver')} activa")
        url = f"http://{local_ip(host)}:{srv.listen_port}{srv.route}"
        print(f"▶ entrada estándar\n   URL: {url}")
        sess.load(app["transportId"], url, "audio/mpeg", "entrada estándar")
        if not sess.monitor():
            sys.exit("✖ la entrada estándar no pudo reproducirse")
    except KeyboardInterrupt:
        print(
            "\n⏱ interrumpido: el audio sigue en el dispositivo hasta terminar"
            " (o hasta que alguien lo corte)"
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
        buf.end()
        srv.shutdown()


def cmd_volume(host, port_cast, level):
    sess = CastSession(host, port_cast)
    if level is None:  # sin argumento: solo consulta
        v = sess.get_status().get("volume", {})
        print(
            f"🔊 volumen: {v.get('level', 0) * 100:.0f}%"
            + ("  ⏺ mute" if v.get("muted") else "")
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
    # el acuse se usa solo para sincronizar; el nivel lo confirmamos con un
    # GET_STATUS fresco porque el estado que viaja asíncrono puede traer el valor anterior
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
        f"🔊 volumen: {v.get('level', lvl) * 100:.0f}%"
        + ("  ⏺ mute" if v.get("muted") else "")
    )


def cmd_stop(host, port_cast):
    sess = CastSession(host, port_cast)
    apps = sess.get_status().get("applications", [])
    if not apps:
        print("ℹ nada en reproducción")
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
    print("✔ stop" + (" confirmado" if m else " enviado"))


def cmd_status(host, port_cast):
    sess = CastSession(host, port_cast)
    st = sess.get_status()
    v = st.get("volume", {})
    print(f"volumen: {v.get('level', 0) * 100:.0f}% (mute: {v.get('muted', False)})")
    apps = st.get("applications", [])
    if not apps:
        print("sin apps activas")
        return
    for a in apps:
        print(
            f"app: {a.get('displayName', a.get('appId'))}  id:{a.get('appId')}"
            f"  sesion:{a.get('sessionId')}  transport:{a.get('transportId')}"
        )
        if a.get("statusText"):
            print(f"   estado: {a['statusText']}")


def main():
    try:  # logs legibles aunque se redirija la salida (nohup, etc.)
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="cast2nest.py",
        description="Envía audio a un Google Cast (Nest Mini, etc.) usando solo "
        "la stdlib de Python (sin pip ni pychromecast). "
        "Sin --host, autodescubre el Cast escaneando la subred.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""ejemplos:
  # una pista (ogg/m4a/etc.: se convierte UNA VEZ a mp3 en ~/.cache/cast2nest;
  #            las siguientes veces arranca al instante desde el cache)
  %(prog)s play ~/Música/sonido.mp3

  # título manual para la pista
  %(prog)s play ~/Música/sonido.ogg "Código y foco"

  # playlist: un directorio, varios archivos/URLs mezclados, o un .m3u/.m3u8
  %(prog)s play ~/Música/caught_in_joy...4h/
  %(prog)s play ~/Música/*.mp3 https://una.radio/stream.mp3
  %(prog)s play ~/Música/coding.m3u

  # pipa desde otra app (ffmpeg reconoce el formato solo: mp3/ogg/flac/webm…)
  yt-dlp -o - "URL" | %(prog)s play -
  cat archivo.ogg | %(prog)s play -

  # pipa de audio CRUDO: --raw (= s16le · 48000 Hz · estéreo)
  SINK=$(pactl get-default-sink)
  pactl record SOURCE="${SINK}.monitor" | %(prog)s play - --raw   # todo lo que suena en la PC
  pactl record SOURCE=$(pactl get-default-source) | %(prog)s play - --raw   # micrófono
  # captura con otros parámetros:
  ... | %(prog)s play - --in-fmt f32le --in-rate 44100 --in-ch 2

control del dispositivo:
  %(prog)s volume        # ver volumen actual (y estado de mute)
  %(prog)s volume 40     # fijar volumen 0..100
  %(prog)s stop          # cortar lo que suena
  %(prog)s status        # volumen + app activa
  %(prog)s --host 192.168.1.138 play …   # sin autodescubrimiento

comportamiento:
  · mientras suena, el proceso queda vivo sirviendo el/los archivo(s) por HTTP (:8099)
  · Ctrl-C: la pista actual sigue en el dispositivo hasta terminar (o `stop`)
  · fin de playlist: se manda STOP y el altavoz queda libre
  · una pista rechazada por el firmware: se saltea y sigue con la próxima
  · diagnósticos HTTP (con Range) en ~/.cache/cast2nest/serve.log
""",
    )
    ap.add_argument("--host", help="IP del dispositivo Cast (si no: autodescubre)")
    ap.add_argument("--cast-port", type=int, default=8009)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser(
        "play",
        help="reproduce 1+ archivo/URL, un directorio "
        "o una playlist .m3u/.m3u8; '-' pipa stdin",
    )
    p.add_argument("media", nargs="+")
    p.add_argument("--http-port", type=int, default=8099)
    p.add_argument(
        "--raw",
        action="store_true",
        help="'-': entrada cruda PCM con default s16le·48000·2 canales "
        "(ajustable a mano con --in-rate/--in-ch)",
    )
    p.add_argument("--in-fmt", help="'-': formato crudo fino (ej. s16le, f32le)")
    p.add_argument("--in-rate", type=int, help="'-': sample rate fino (ej. 44100)")
    p.add_argument("--in-ch", type=int, help="'-': canales finos (ej. 2)")
    p = sub.add_parser("volume", help="nivel 0..100; sin valor: solo consulta")
    p.add_argument("level", nargs="?", type=float)
    p = sub.add_parser("stop", help="corta la reproducción")
    p = sub.add_parser("status", help="estado del dispositivo")
    args = ap.parse_args()

    host = args.host or pick_host()
    print(f"→ Cast: {host}:{args.cast_port}")
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
        sys.exit(f"✖ conexión: {e}")
    except ssl.SSLError as e:
        sys.exit(f"✖ TLS: {e}")
