# Changelog

All notable changes to this project are documented in this file.
The format follows Keep a Changelog and the project uses Semantic Versioning.

## [0.1.0] - 2026-11-11

### Added

- Standard-library Google Cast audio playback, playlists, discovery, volume,
  status, stop, and standard-input streaming.
- Unit tests and CI for Python 3.10, 3.12, and 3.13.
- README and GNU GPL-3.0-or-later licensing.

### Fixed

- Apply route and device-IP authorization to GET and HEAD requests.
- Refuse HTTP serving when the Cast device IP cannot be resolved.
- Return HTTP 416 for unsatisfiable byte ranges.
- Report Cast load and decode failures as unsuccessful playback.
- Return a failure exit status when standard-input playback fails.
- Use the `cast2nest` name consistently in the CLI, metadata, and cache.
