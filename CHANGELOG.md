# Changelog

All versions use Semantic Versioning. This file documents only the
functionality available in each release.

## [0.1.0] - 2026-11-11

### Features

- Playback of MP3, FLAC, and WAV files on Google Cast devices.
- Conversion and caching of other audio formats via FFmpeg.
- Playback of directories, M3U/M3U8 playlists, URLs, and standard input.
- Raw PCM streaming with configurable format, sample rate, and channels.
- Automatic discovery on the local subnet and manual selection with `--host`.
- Volume control, status query, and stopping playback.
- Local HTTP server with random paths, device IP filtering, and byte range
  support.
- Plain-text terminal output.
- Animated guide explaining the audio's path through the local network.
- 26 unit tests, static checks, and CI on Python 3.10, 3.12, and 3.13.
- Automated GitHub Releases from version tags.
- `GPL-3.0-or-later` license.
