"""CLI entry: `python -m smartthings_pushover [--test-notify | --dump]`."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from . import __version__
from .config import ALL_EVENTS, Config, ConfigError
from .pushover import Notification, PushoverError, PushoverSender, send_once


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # The library's DTLS layer is chatty at DEBUG; keep it to INFO unless
    # explicitly asked for.
    if level != "DEBUG":
        logging.getLogger("smartthings_local").setLevel(logging.INFO)


def _test_notify(cfg: Config) -> int:
    log = logging.getLogger("main")
    note = Notification(
        message=f"Test notification from smartthings-pushover {__version__}.",
        title=cfg.washer_name, priority=cfg.pushover_priority,
        sound=cfg.pushover_sound, device=cfg.pushover_device,
        timestamp=int(time.time()))
    try:
        result = send_once(cfg.pushover_token, cfg.pushover_user, note)
    except PushoverError as e:
        log.error("Pushover rejected the test message: %s", e)
        return 2
    except Exception as e:  # noqa: BLE001
        log.error("could not reach Pushover: %s", e)
        return 3
    log.info("Pushover accepted the test message (request %s)",
             result.get("request"))
    return 0


def _dump(cfg: Config) -> int:
    """Connect once, read /device/0, print the flattened state, exit.
    Handy for confirming certs/IP and for finding the course code that
    matches the dial position (see COURSE_NAMES)."""
    from . import washer
    from .bridge import WasherBridge

    class _NullSender:
        sent_count = failed_count = 0
        def submit(self, *a, **k): pass
        def start(self): pass
        def stop(self, *a, **k): pass

    bridge = WasherBridge(cfg, _NullSender())  # type: ignore[arg-type]
    from smartthings_local.protocol.dtls_session import DtlsCoapSession
    port = bridge._resolve_port()
    sess = DtlsCoapSession(cfg.washer_ip, port, auth=bridge.auth,
                           local_port=cfg.dtls_local_port)
    sess.connect()
    sess.start_reader()
    try:
        bridge._seed(sess)
    finally:
        sess.close()
    links = bridge.cache.snapshot()
    state = washer.flatten(links)
    print()
    for k, v in state.__dict__.items():
        print(f"  {k:18} {v}")
    print(f"  {'remaining':18} {state.remaining_hms()}")
    _print_courses(cfg, state, washer.supported_course_codes(links))
    return 0


def _print_courses(cfg: Config, state, codes: list[str]) -> None:
    """The washer never sends course *names*, only the hex code of the
    dial position, so the mapping has to be built by hand: turn the dial
    to a programme, run --dump, note the code. This prints every code
    the firmware advertises so you can see how many are still unnamed."""
    if state.course:
        label = cfg.course_names.get(state.course)
        print(f"\n  Current course code: {state.course}"
              + (f" -> {label}" if label else "  (unnamed)"))
    if not codes:
        if state.course and state.course not in cfg.course_names:
            print("  add to COURSE_NAMES, e.g. "
                  f"COURSE_NAMES=\"{state.course}=Cotton\"")
        return
    print(f"\n  Courses advertised by the washer ({len(codes)}):")
    for code in codes:
        mark = "*" if code == state.course else " "
        label = cfg.course_names.get(code)
        print(f"   {mark} {code}  {label if label else '(unnamed)'}")
    unnamed = [c for c in codes if c not in cfg.course_names]
    unknown = sorted(set(cfg.course_names) - set(codes))
    if unnamed:
        print(f"\n  {len(unnamed)} unnamed. Turn the dial to each, run --dump,"
              " and add the code:")
        print("    COURSE_NAMES=" + ",".join(
            f"{c}={cfg.course_names[c]}" for c in codes if c in cfg.course_names)
            + ("," if cfg.course_names else "")
            + ",".join(f"{c}=..." for c in unnamed))
    else:
        print("\n  All courses named.")
    if unknown:
        print(f"  COURSE_NAMES has codes the washer does not advertise: "
              f"{', '.join(unknown)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="smartthings_pushover",
        description="Forward Samsung washing machine events to Pushover.",
        epilog=f"Events: {', '.join(ALL_EVENTS)}. Configure via env vars; "
               "see README.")
    ap.add_argument("--test-notify", action="store_true",
                    help="send one test message to Pushover and exit")
    ap.add_argument("--dump", action="store_true",
                    help="connect to the washer once, print its state, exit")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

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
        return _dump(cfg)

    from .bridge import WasherBridge

    sender = PushoverSender(
        cfg.pushover_token, cfg.pushover_user,
        default_device=cfg.pushover_device,
        default_sound=cfg.pushover_sound,
        default_priority=cfg.pushover_priority,
        logger=logging.getLogger("pushover"))
    sender.start()
    bridge = WasherBridge(cfg, sender, logger=logging.getLogger("washer"))

    def _on_signal(signum, _frame):
        log.info("signal %d: shutting down", signum)
        bridge.request_stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log.info("smartthings-pushover %s: %s @ %s:%s -> Pushover; events=%s",
             __version__, cfg.washer_name, cfg.washer_ip,
             cfg.washer_port or "auto", ",".join(sorted(cfg.events)))
    try:
        bridge.run_forever()
    finally:
        sender.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
