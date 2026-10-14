# CLAUDE.md

Checks: `uv run pytest -q`, `uv run ruff check .`, `uv run mypy smartthings_pushover`.
(`ruff format --check` already fails on existing files; don't add new diffs.)

## Formatting an unformatted notification

The user will bring a screenshot or text of a notification that shows a raw
`Details: items: {...}` line. That is the fallback in
`alarms.alarm_fields()` when no `code=XX` could be decoded. The goal is
always: clean `Label: value` fields, never hide errors, keep the raw
fallback for shapes still unknown.

1. **Read the raw shape.** Note each `key=value` in the item, especially
   `code`, `state` (`Created` = raised, `Deleted` = cleared) and
   `triggeredTime` (appliance UTC; convert to `tz` with `alarms._local_time`).
2. **Decide what it is.**
   - A new error code: add it to `ALARM_CODES` in `alarms.py`
     (sentence-case meaning, manual wording). `_CODE_RE` takes up to
     7 characters after an optional `ErrorCode_` prefix.
   - A new item shape or state: add a predicate next to `is_cleared()`
     and a `*_fields()` builder next to `cleared_fields()`. If the item
     has lost its code, recover it from `was.alarms` (the previous
     snapshot), as `cleared_codes()` does.
3. **Wire it in `events.detect()`** under `# ---- alarms`. Errors stay the
   `alarm` kind (high priority, exempt from quiet hours). Anything that is
   not an error gets its own event kind:
   - add it to `EventKind` (and to `DEFAULT_EVENTS` if it should be on
     by default) in `config.py`;
   - give it a separate `AlarmThrottle` in `bridge.py` and include it in
     the `_dispatch` throttle check and the raw-alarm log line;
   - set `dedupe_key` from codes/state only, never `triggeredTime` (it
     changes on every re-check).
4. **Tests.** Paste the captured shape verbatim into `tests/test_alarms.py`
   (fields) and `tests/test_events.py` (event kind, title, fields,
   dedupe key), with the capture date and appliance in the docstring.
   Use a non-UTC `tz` so the time conversion is exercised.
5. **Docs.** Update the event table and default `EVENTS` in `README.md`,
   `.env.example`, and add a CHANGELOG entry under `[Unreleased]`. Add the
   captured shape as a comment beside the regex/predicate that handles it.

Titles are `"{name}: Title Case"` (`Tumble Dryer: Error Cleared`); status
values are Title Case and meanings are sentence case.

Captured so far:
- `code=ErrorCode_DC, state=Created` → `alarm`, Code DC (WW80, 2026-09-10)
- `code=ErrorCode, state=Deleted` → `alarm_cleared`, code from the
  previous alarm (DV80, 2026-10-05)

## Versioning and releases

The version lives only in `pyproject.toml` (`version = ...`). The app reads
it at runtime via `importlib.metadata` (`--version`, User-Agent, the test
and startup notifications); never hard-code it anywhere else. Bump it with
`uv version`, which also updates `uv.lock`, so `uv lock --check` stays green.
SemVer: new event kinds or fields → minor; fixes → patch.

`main` is protected: every change goes through a branch and a pull request.

1. `git checkout -b release-X.Y.Z` from an up-to-date `main` (release
   commits can ride on the same branch as the changes they ship).
2. `uv version --bump minor` (or `patch` / `major`, or `uv version X.Y.Z`).
3. `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`,
   add a fresh empty `## [Unreleased]` above it, and update the compare
   links at the bottom (`[Unreleased]` → `vX.Y.Z...HEAD`, new
   `[X.Y.Z]` → `vPREV...vX.Y.Z`).
4. Run the checks, commit as `Release X.Y.Z`, `git push -u origin
   release-X.Y.Z`, open a PR (`gh` isn't installed locally; give the user
   `https://github.com/ck3mp/smartthings-pushover/pull/new/release-X.Y.Z`).
5. After the user merges: `git checkout main && git pull`, then tag the
   merge commit and push only the tag:
   `git tag -a vX.Y.Z -m "Release X.Y.Z" && git push origin vX.Y.Z`.
   CI checks the tag equals `uv version --short` and publishes the image
   as `:X.Y.Z`, `:X.Y` and `:latest`.
