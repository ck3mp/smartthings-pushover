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
    state = bridge.current_state()
    print()
    for k, v in state.__dict__.items():
        print(f"  {k:18} {v}")
    print(f"  {'remaining':18} {state.remaining_hms()}")
    if state.course:
        label = cfg.course_names.get(state.course)
        print(f"\n  Current course code: {state.course}"
              + (f" -> {label}" if label else
                 "  (add to COURSE_NAMES, e.g. COURSE_NAMES=\""
                 f"{state.course}=Cotton\")"))
    return 0


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
