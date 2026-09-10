"""CLI entry: `python -m smartthings_pushover [--test-notify | --dump [washer|dryer]]`."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from . import __version__
from .config import ALL_EVENTS, KINDS, ApplianceConfig, Config, ConfigError
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
    title = " / ".join(a.name for a in cfg.appliances)
    note = Notification(
        message=f"Test notification from smartthings-pushover {__version__}.",
        title=title, priority=cfg.pushover_priority,
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


class _NullSender:
    sent_count = failed_count = 0

    def submit(self, *a, **k): pass
    def start(self): pass
    def stop(self, *a, **k): pass


def _dump(cfg: Config, kinds: list[str]) -> int:
    """Connect to each selected appliance once, read /device/0, print the
    flattened state and the course table, exit. Handy for confirming
    certs/IP and for building *_COURSE_NAMES."""
    from . import appliance
    from .bridge import ApplianceBridge
    from smartthings_local.protocol.dtls_session import DtlsCoapSession

    rc = 0
    for app in cfg.appliances:
        if app.kind not in kinds:
            continue
        print(f"\n== {app.kind}: {app.name} @ {app.ip} ==")
        bridge = ApplianceBridge(app, cfg, _NullSender())  # type: ignore[arg-type]
        try:
            port = bridge._resolve_port()
            sess = DtlsCoapSession(app.ip, port, auth=bridge.auth,
                                   local_port=app.dtls_local_port)
            sess.connect()
            sess.start_reader()
            try:
                bridge._seed(sess)
            finally:
                sess.close()
        except Exception as e:  # noqa: BLE001
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


def _print_courses(app: ApplianceConfig, state, codes: list[str]) -> None:
    """The appliance never sends course *names*, only the hex code of the
    dial position, so the mapping has to be built by hand: turn the dial
    to a programme, run --dump, note the code. This prints every code
    the firmware advertises so you can see how many are still unnamed."""
    var = f"{app.prefix}_COURSE_NAMES"
    if state.course:
        label = app.course_names.get(state.course)
        print(f"\n  Current course code: {state.course}"
              + (f" -> {label}" if label else "  (unnamed)"))
    if not codes:
        if state.course and state.course not in app.course_names:
            print(f"  add to {var}, e.g. {var}=\"{state.course}=Cotton\"")
        return
    print(f"\n  Courses advertised by the {app.kind} ({len(codes)}):")
    for code in codes:
        mark = "*" if code == state.course else " "
        label = app.course_names.get(code)
        print(f"   {mark} {code}  {label if label else '(unnamed)'}")
    unnamed = [c for c in codes if c not in app.course_names]
    unknown = sorted(set(app.course_names) - set(codes))
    if unnamed:
        print(f"\n  {len(unnamed)} unnamed. Turn the dial to each, run --dump,"
              " and add the code:")
        print(f"    {var}=" + ",".join(
            f"{c}={app.course_names[c]}" for c in codes if c in app.course_names)
            + ("," if app.course_names else "")
            + ",".join(f"{c}=..." for c in unnamed))
    else:
        print("\n  All courses named.")
    if unknown:
        print(f"  {var} has codes the {app.kind} does not advertise: "
              f"{', '.join(unknown)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="smartthings_pushover",
        description="Forward Samsung washer / dryer events to Pushover.",
        epilog=f"Events: {', '.join(ALL_EVENTS)}. Configure via env vars; "
               "see README.")
    ap.add_argument("--test-notify", action="store_true",
                    help="send one test message to Pushover and exit")
    ap.add_argument("--dump", nargs="?", const="all", choices=("all",) + KINDS,
                    metavar="|".join(KINDS),
                    help="connect to the enabled appliances (or just one) once, "
                         "print their state and course table, exit")
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
        kinds = list(KINDS) if args.dump == "all" else [args.dump]
        if not any(a.kind in kinds for a in cfg.appliances):
            print(f"config error: no enabled appliance matches --dump {args.dump}",
                  file=sys.stderr)
            return 1
        return _dump(cfg, kinds)

    from .bridge import ApplianceBridge

    sender = PushoverSender(
        cfg.pushover_token, cfg.pushover_user,
        default_device=cfg.pushover_device,
        default_sound=cfg.pushover_sound,
        default_priority=cfg.pushover_priority,
        logger=logging.getLogger("pushover"))
    sender.start()
    bridges = [ApplianceBridge(app, cfg, sender, logger=logging.getLogger(app.kind))
               for app in cfg.appliances]

    def _on_signal(signum, _frame):
        log.info("signal %d: shutting down", signum)
        for b in bridges:
            b.request_stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log.info("smartthings-pushover %s -> Pushover; events=%s",
             __version__, ",".join(sorted(cfg.events)))
    threads = []
    for b in bridges:
        log.info("%s: %s @ %s:%s", b.app.kind, b.app.name, b.app.ip,
                 b.app.port or "auto")
        t = threading.Thread(target=b.run_forever, name=b.app.kind, daemon=True)
        t.start()
        threads.append(t)
    try:
        # Wait on the bridges; signal handlers only run on the main thread,
        # so poll rather than block forever in join().
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    finally:
        for b in bridges:
            b.request_stop()
        sender.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
