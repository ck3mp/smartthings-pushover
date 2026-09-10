"""Command line: run the bridge, or one of the one-shot helpers.

    smartthings-pushover                      run until SIGTERM/SIGINT
    smartthings-pushover --test-notify        send one Pushover message
    smartthings-pushover --dump [washer|dryer] read each appliance once
    smartthings-pushover --healthcheck        exit 0 if the heartbeat is fresh

Add `--env-file .env` to any of them to read settings from a file.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import MutableMapping
from typing import Any

from . import __version__
from .config import ALL_EVENTS, ApplianceConfig, Config, ConfigError
from .health import Heartbeat
from .kinds import KINDS
from .pushover import Notification, NullSender, PushoverError, PushoverSender, send_once

HEARTBEAT_EVERY_S = 10.0
HEARTBEAT_MAX_AGE_S = 60.0
DEFAULT_HEARTBEAT_PATH = "/tmp/smartthings-pushover.heartbeat"  # per-container tmpfs


def load_env_file(path: str, environ: MutableMapping[str, str] | None = None) -> int:
    """Load `KEY=VALUE` lines the way `docker run --env-file` does: the value
    is everything after the first `=`, verbatim (no quote stripping, so
    course names with spaces and apostrophes survive); blank lines and
    `#` comments are skipped; variables already in the environment win.
    Returns how many variables were set."""
    env = os.environ if environ is None else environ
    count = 0
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in env:
                env[key] = value.strip()
                count += 1
    return count


def _setup_logging(level: str) -> None:
    root_level = getattr(logging, level, logging.INFO)
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # The library's DTLS layer is chatty at DEBUG; never let it go below
    # INFO, and never let it be louder than what the user asked for.
    logging.getLogger("smartthings_local").setLevel(max(logging.INFO, root_level))


def _test_notify(cfg: Config) -> int:
    log = logging.getLogger("main")
    title = " / ".join(a.name for a in cfg.appliances)
    note = Notification(
        message=f"<b>Status:</b> Test Notification\n<b>Version:</b> {__version__}",
        title=title,
        priority=cfg.pushover_priority,
        sound=cfg.pushover_sound,
        device=cfg.pushover_device,
        timestamp=int(time.time()),
        html=True,
    )
    try:
        result = send_once(cfg.pushover_token, cfg.pushover_user, note)
    except PushoverError as e:
        log.error("Pushover rejected the test message: %s", e)
        return 2
    except Exception as e:  # noqa: BLE001 - any transport failure is reported the same way
        log.error("could not reach Pushover: %s", e)
        return 3
    log.info("Pushover accepted the test message (request %s)", result.get("request"))
    return 0


def _dump(cfg: Config, kinds: list[str]) -> int:
    """Connect to each selected appliance once, read /device/0, print the
    flattened state and the course table, exit. Handy for confirming
    certs/IP and for building *_COURSE_NAMES."""
    from . import appliance
    from .bridge import ApplianceBridge

    # Nothing is sent from a dump, so don't log a "notify startup" either.
    cfg = dataclasses.replace(cfg, events=cfg.events - {"startup"})
    rc = 0
    for app in cfg.appliances:
        if app.kind not in kinds:
            continue
        print(f"\n== {app.kind}: {app.name} @ {app.ip} ==")
        # A one-shot read must not touch the running bridge's saved cycle state.
        bridge = ApplianceBridge(app, cfg, NullSender(), persist=False)
        try:
            sess = bridge.open_session()
            try:
                bridge.seed(sess)
            finally:
                sess.close()
        except Exception as e:  # noqa: BLE001 - report and move on to the next appliance
            print(f"  error: {e}")
            rc = 4
            continue
        links = bridge.cache.snapshot()
        state = appliance.flatten(links)
        print()
        for k, v in state.__dict__.items():
            print(f"  {k:18} {v}")
        print(f"  {'remaining':18} {state.remaining_hms()}")
        _print_courses(app, state, appliance.supported_course_codes(links))
    return rc


def _print_courses(app: ApplianceConfig, state: Any, codes: list[str]) -> None:
    """The appliance never sends course *names*, only the hex code of the
    dial position, so the mapping has to be built by hand: turn the dial
    to a programme, run --dump, note the code. This prints every code
    the firmware advertises so you can see how many are still unnamed."""
    var = f"{app.prefix}_COURSE_NAMES"
    if state.course:
        label = app.course_names.get(state.course)
        print(
            f"\n  Current course code: {state.course}"
            + (f" -> {label}" if label else "  (unnamed)")
        )
    if not codes:
        if state.course and state.course not in app.course_names:
            print(f'  add to {var}, e.g. {var}="{state.course}=Cotton"')
        return
    print(f"\n  Courses advertised by the {app.kind} ({len(codes)}):")
    for code in codes:
        mark = "*" if code == state.course else " "
        label = app.course_names.get(code)
        print(f"   {mark} {code}  {label if label else '(unnamed)'}")
    unnamed = [c for c in codes if c not in app.course_names]
    unknown = sorted(set(app.course_names) - set(codes))
    if unnamed:
        print(f"\n  {len(unnamed)} unnamed. Turn the dial to each, run --dump, and add the code:")
        named = ",".join(f"{c}={app.course_names[c]}" for c in codes if c in app.course_names)
        todo = ",".join(f"{c}=..." for c in unnamed)
        print(f"    {var}=" + ",".join(p for p in (named, todo) if p))
    else:
        print("\n  All courses named.")
    if unknown:
        print(f"  {var} has codes the {app.kind} does not advertise: {', '.join(unknown)}")


def _healthcheck() -> int:
    path = os.environ.get("HEARTBEAT_PATH") or DEFAULT_HEARTBEAT_PATH
    return 0 if Heartbeat.is_fresh(path, HEARTBEAT_MAX_AGE_S) else 1


def _run(cfg: Config, log: logging.Logger) -> int:
    from .bridge import ApplianceBridge

    sender = PushoverSender(
        cfg.pushover_token,
        cfg.pushover_user,
        default_device=cfg.pushover_device,
        default_sound=cfg.pushover_sound,
        default_priority=cfg.pushover_priority,
        logger=logging.getLogger("pushover"),
    )
    sender.start()
    bridges = [
        ApplianceBridge(app, cfg, sender, logger=logging.getLogger(app.kind))
        for app in cfg.appliances
    ]
    heartbeat = Heartbeat(cfg.heartbeat_path or DEFAULT_HEARTBEAT_PATH)
    stop_requested = threading.Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("signal %d: shutting down", signum)
        stop_requested.set()
        for b in bridges:
            b.request_stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log.info(
        "smartthings-pushover %s -> Pushover; events=%s",
        __version__,
        ",".join(sorted(cfg.events)),
    )
    if cfg.quiet_hours:
        s, e = cfg.quiet_hours
        log.info(
            "quiet hours %02d:%02d-%02d:%02d -> priority %d",
            s // 60, s % 60, e // 60, e % 60, cfg.quiet_priority,
        )
    threads = []
    for b in bridges:
        log.info("%s: %s @ %s:%s", b.app.kind, b.app.name, b.app.ip, b.app.port or "auto")
        t = threading.Thread(target=b.run_forever, name=b.app.kind, daemon=True)
        t.start()
        threads.append(t)

    rc = 0
    next_beat = 0.0
    try:
        # Wait on the bridges; signal handlers only run on the main thread,
        # so poll rather than block forever in join().
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
            now = time.monotonic()
            if now >= next_beat and all(t.is_alive() for t in threads):
                heartbeat.touch()
                next_beat = now + HEARTBEAT_EVERY_S
        if not stop_requested.is_set():
            dead = [t.name for t in threads if not t.is_alive()]
            log.error("bridge thread(s) exited unexpectedly: %s", ", ".join(dead))
            rc = 1
    finally:
        for b in bridges:
            b.request_stop()
        sender.stop()
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="smartthings-pushover",
        description="Forward Samsung washer / dryer events to Pushover.",
        epilog=f"Events: {', '.join(ALL_EVENTS)}. Configure via env vars; see README.",
    )
    ap.add_argument(
        "--test-notify", action="store_true", help="send one test message to Pushover and exit"
    )
    ap.add_argument(
        "--dump",
        nargs="?",
        const="all",
        choices=("all", *KINDS),
        metavar="|".join(KINDS),
        help="connect to the enabled appliances (or just one) once, "
        "print their state and course table, exit",
    )
    ap.add_argument(
        "--healthcheck",
        action="store_true",
        help="exit 0 if the running bridge's heartbeat is fresh (Docker HEALTHCHECK)",
    )
    ap.add_argument(
        "--env-file",
        metavar="PATH",
        help="load KEY=VALUE settings from this file first (Docker --env-file syntax; "
        "variables already set in the environment win)",
    )
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    if args.env_file:
        try:
            load_env_file(args.env_file)
        except OSError as e:
            print(f"config error: cannot read {args.env_file}: {e.strerror}", file=sys.stderr)
            return 1

    if args.healthcheck:
        return _healthcheck()

    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    _setup_logging(cfg.log_level)
    log = logging.getLogger("main")

    if args.test_notify:
        return _test_notify(cfg)
    if args.dump:
        kinds = list(KINDS) if args.dump == "all" else [args.dump]
        if not any(a.kind in kinds for a in cfg.appliances):
            print(f"config error: no enabled appliance matches --dump {args.dump}", file=sys.stderr)
            return 1
        return _dump(cfg, kinds)
    return _run(cfg, log)
