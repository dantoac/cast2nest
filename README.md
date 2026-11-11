# cast2nest

Send local audio files, playlists, URLs, and standard input to a Google Cast
device. The Cast V2 protocol implementation uses only the Python standard
library; `pychromecast` is not required.

## Requirements

- Python 3.10 or newer.
- A Google Cast device on the same trusted LAN.
- FFmpeg and ffprobe for metadata, transcoding, and standard input.
- An FFmpeg build with `libmp3lame` for live standard-input streaming.

MP3, FLAC, and WAV files are served without conversion. Other supported local
audio formats are converted once and cached. URL playback sends the URL directly
to the Cast device and labels it as `audio/mpeg`.

## Install

```sh
git clone https://github.com/dantoac/cast2nest.git
cd cast2nest
./cast2nest.py --help
```

No Python package installation is required. On macOS, install FFmpeg with
`brew install ffmpeg`. On Debian or Ubuntu, use `sudo apt install ffmpeg`.

## Usage

Play one file:

```sh
./cast2nest.py play ~/Music/sound.mp3
```

Play several files, a directory, or an M3U playlist:

```sh
./cast2nest.py play ~/Music/focus/
./cast2nest.py play first.mp3 second.flac "Second track title"
./cast2nest.py play ~/Music/focus.m3u8
```

Play an Internet stream:

```sh
./cast2nest.py play https://example.org/stream.mp3
```

Pipe audio through FFmpeg:

```sh
yt-dlp -o - "URL" | ./cast2nest.py play -
cat audio.ogg | ./cast2nest.py play -
```

Pipe raw PCM audio:

```sh
pactl record SOURCE=@DEFAULT_SOURCE@ | ./cast2nest.py play - --raw
```

The raw defaults are signed 16-bit little-endian PCM, 48000 Hz, and stereo.
Use `--in-fmt`, `--in-rate`, and `--in-ch` to override them.

Control the device:

```sh
./cast2nest.py volume
./cast2nest.py volume 40
./cast2nest.py status
./cast2nest.py stop
```

Use `--host` to skip discovery and `--cast-port` to change the default Cast
port, 8009:

```sh
./cast2nest.py --host 192.168.1.50 status
```

## Operation and storage

The process must remain alive while it serves local media. The default HTTP
port is 8099; if it is occupied, the program selects a free port. `Ctrl-C`
stops the local server, but the current track can continue on the device.
A completed playlist closes the Default Media Receiver application.

Converted audio is stored in `~/.cache/cast2nest`. HTTP diagnostics, including
request paths and file names, are stored in `~/.cache/cast2nest/serve.log`.
Delete that directory to remove the cache and logs.

## Network security

Use this tool only on a trusted LAN.

- Discovery scans TCP port 8009 on the local `/24` network.
- Local media is served over unencrypted HTTP while playback is active.
- File and stream URLs contain random tokens. GET and HEAD requests are limited
  to registered routes and the resolved Cast device IP.
- Failure to resolve the Cast IP aborts file serving instead of opening access.
- Cast TLS certificate verification is disabled for compatibility with device
  certificates. Device authentication is limited.

These controls reduce accidental exposure; they do not protect against an
active attacker on the local network.

## Development

Run the test suite without Cast hardware or network access:

```sh
python3 -m unittest discover -s tests -v
```

Optional static checks:

```sh
python3 -m pip install ruff mypy
ruff check .
ruff format --check .
mypy --ignore-missing-imports cast2nest.py
```

CI runs the tests on Python 3.10, 3.12, and 3.13, plus Ruff and mypy.

## License

Copyright (C) 2026 Daniel Aguayo.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

SPDX identifier: `GPL-3.0-or-later`. See [LICENSE](LICENSE).

Google Cast, Chromecast, and Google Nest are trademarks of Google LLC. This
project is not affiliated with or endorsed by Google.
