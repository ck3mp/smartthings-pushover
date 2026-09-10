# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.1] - 2026-09-10

### Changed

- Notifications are now factual `Label: value` fields with bold labels
  (Pushover HTML), for example `Status: Spinning`, `Time Remaining: 14m`,
  `Percentage Complete: 31%`. Settings are separate fields (Temperature,
  Spin, Rinses, Dry Level, Dry Time). No prose, no emojis.
- Status values are Title Case (`Powered On`, `Delayed Start Armed`) and
  alarm meanings are sentence case, as written in the code table.
- Alarm notifications carry `Code`, `Meaning` and `Raised`; the raw fields
  are appended only when the code is unknown. `Raised` is converted from
  the appliance's UTC stamp to the configured `TZ`.
- The unknown-phase fallback shows the appliance's raw progress value.

### Added

- `Estimated Finish` (`HH:MM`, 24-hour, in `TZ`) under `Time Remaining` on
  Started, Resumed, phase-change and Delayed Start Armed messages.
- `--env-file PATH` on the CLI, reading the file with Docker's
  `--env-file` rules so course names with spaces and apostrophes work.
- This changelog.

### Fixed

- A fresh start while a cycle was already running could send `Started`
  (and re-send a standing alarm) if OBSERVE registration replies arrived
  before the initial tree read, or out of order. Evaluation is now held
  from the first subscription until the seed has the whole tree.
- Heartbeat write failures are logged once instead of silently failing the
  container health check.
- A shutdown that cancels a DTLS handshake no longer counts as a session
  error in the health line.
- `--dump` no longer logs a misleading `notify startup` line and never
  touches the persisted cycle state.

## [1.0.0] - 2026-09-10

First public release.

### Added

- One DTLS/CoAP session per appliance (washer and dryer) with tiered
  polling, keepalive and OBSERVE refresh, rebuilt on reconnect with
  jittered backoff; a stop request cancels a handshake in progress.
- Event detection for cycle start, phases, pause, resume, finish, cancel,
  Delay End (armed, started, cancelled), power, remote control, child lock,
  alarms, offline and online, with per-event selection via `EVENTS`.
- Cycle tracker persisted in `STATE_DIR` so the duration survives a restart.
- Quiet hours (`QUIET_HOURS`, `QUIET_PRIORITY`) and per-event
  `EVENT_PRIORITIES`; alarm rate limiting (`ALARM_REPEAT_S`).
- Pushover client with retries, monthly-quota (429) handling, and
  `PUSHOVER_TOKEN_FILE` / `PUSHOVER_USER_FILE` for Docker secrets.
- Table-driven appliance kinds (`kinds.py`); course-name mapping with
  `--dump` to list the codes an appliance advertises.
- Two-stage, digest-pinned Docker image running as an unprivileged user
  with a `HEALTHCHECK`; multi-arch publish to GHCR; SHA-pinned CI with
  lockfile check, ruff, mypy and pytest; Dependabot for uv, actions and
  docker.
- Verified live against a WW80CGC04DAEEU washer and a DV80CGC0B0AEEU
  dryer, including a real door-open alarm and Delay End.

[Unreleased]: https://github.com/ck3mp/smartthings-pushover/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/ck3mp/smartthings-pushover/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/ck3mp/smartthings-pushover/releases/tag/v1.0.0
