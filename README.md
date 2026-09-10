# smartthings-pushover

> Built with the assistance of [Claude Fable 5.1](https://www.anthropic.com/claude/fable),
> Anthropic's AI model, working in Claude Code. Claude carried out the code
> review, wrote the fixes, tests, packaging and documentation, and ran the
> live tests against the appliances alongside the author.

Pushover notifications from a Samsung washing machine and/or tumble dryer,
straight off your LAN. A small Docker container talks to the appliance over
its local OCF endpoint (CoAP over DTLS) and tells your phone when a cycle
starts, pauses, finishes, is cancelled, or the machine raises an alarm.

No SmartThings cloud, no MQTT broker, no Home Assistant. One process, one
encrypted session per appliance, outbound HTTPS to Pushover, nothing
listening.

It builds on the [smartthings-local](https://github.com/QuiteYellow/SmartThings-Local)
library and was written and tested against a **WW80CGC04DAEEU** washer and a
**DV80CGC0B0AEEU** dryer (both `DA_WM_TP1_21_COMMON` firmware). Any Samsung
laundry appliance on the same firmware family that accepts the client
certificate below should work; the same certificate works for both machines.

## Quick start

You need Docker on a machine on the same LAN as the appliances, a Pushover
application token and user key, and the client certificate pair from
[step 1](#1-mint-a-client-certificate-once) (`client_fullchain.pem` and
`client.key`). Then:

```sh
mkdir -p smartthings-pushover/certs smartthings-pushover/data
cd smartthings-pushover
cp /path/to/client_fullchain.pem /path/to/client.key certs/
chown 1000:1000 data
curl -fsSLO https://raw.githubusercontent.com/ck3mp/smartthings-pushover/main/docker-compose.yml
```

Open `docker-compose.yml` and set `WASHER_IP` and/or `DRYER_IP`,
`PUSHOVER_TOKEN` and `PUSHOVER_USER`. Leave everything else as it is. Then:

```sh
docker compose up -d
docker compose logs -f
```

Within a few seconds each appliance should log `DTLS connected` and
`seeded`. Start a cycle and your phone gets `Status: Started` with the
programme, settings and time remaining; when it ends, `Status: Complete`
with the duration. The rest of this document explains the certificate,
every setting, and what to expect from the machines.

## What you need

- A Docker host on the same LAN as the appliances (a NAS, a Raspberry Pi,
  a home server). The container uses host networking so it can reach the
  appliance's UDP port directly.
- A [Pushover](https://pushover.net) account: an **application token**
  (create an application) and your **user key**.
- A client certificate the appliance trusts (next section).
- The appliances' LAN addresses. Give them DHCP reservations so they don't
  move.

## 1. Mint a client certificate (once)

The appliance only talks to clients that present a certificate derived from
Samsung's AC14K_M chain. SmartThings-Local's `setup_cert.py` produces one
and tests it against the appliance; follow *Part 2* of that project's
README. From its checkout:

```sh
TARGET_IP=192.168.1.50 python setup_cert.py --test
```

You need two files from its `certs/` directory:

- `client_fullchain.pem`
- `client.key`

Keep them private. They are the only credential the appliance checks.

## 2. Run it with Docker

Every push to `main` builds a multi-arch image (amd64 + arm64) and
publishes it to GitHub Container Registry as
`ghcr.io/ck3mp/smartthings-pushover:latest`; version tags publish
`:x.y.z` and `:x.y` as well (see
[.github/workflows/publish.yml](.github/workflows/publish.yml); a fork
publishes under its own owner). The image contains no certificates or
secrets, runs as an unprivileged user, and has a `HEALTHCHECK`.

On the Docker host:

1. Make a directory for the deployment and, inside it, `certs/` holding
   the two certificate files and an empty `data/` for runtime state. The
   container runs as uid 1000, so `data/` must be writable by that uid:

   ```sh
   mkdir -p smartthings-pushover/certs smartthings-pushover/data
   chown 1000:1000 smartthings-pushover/data
   ```

2. Copy [docker-compose.yml](docker-compose.yml) into that directory and
   fill in the `environment:` block. The minimum is `WASHER_IP` and/or
   `DRYER_IP`, `PUSHOVER_TOKEN` and `PUSHOVER_USER`; everything else has a
   sensible default. The course-name lists shipped in the file are correct
   for the two tested models.

3. Start it and watch the first connection:

   ```sh
   docker compose pull
   docker compose up -d
   docker compose logs -f
   ```

   You should see each appliance discovered, connected, identified and
   seeded within a few seconds:

   ```
   INFO    main      smartthings-pushover -> Pushover; events=alarm,cycle_cancelled,cycle_finished,cycle_paused,cycle_scheduled,cycle_started
   INFO    main      washer: Washing machine @ 192.168.1.50:auto
   INFO    main      dryer: Tumble dryer @ 192.168.1.51:auto
   INFO    washer    discovered DTLS port 49154
   INFO    dryer     discovered DTLS port 49154
   INFO    washer    DTLS connected to 192.168.1.50:49154
   INFO    dryer     DTLS connected to 192.168.1.51:49154
   INFO    washer    identified DA_WM_TP1_21_COMMON_WW5000C/DC92-03495A_906C serial=…
   INFO    dryer     identified DA_WM_TP1_21_COMMON_DV5000C/DC92-03499A_150C serial=…
   INFO    washer    seeded 33 resources; state=Ready progress=None remaining=02:36:00
   INFO    dryer     seeded 30 resources; state=Ready progress=None remaining=03:10:00
   ```

   A health line per appliance follows every five minutes with connection,
   poll and notification counters.

4. Check Pushover end to end:

   ```sh
   docker compose run --rm smartthings-pushover smartthings-pushover --test-notify
   ```

Upgrade later with `docker compose pull && docker compose up -d`.

### Using Docker secrets for the Pushover keys

Instead of putting the token and user key in the compose file, point
`PUSHOVER_TOKEN_FILE` and `PUSHOVER_USER_FILE` at files (for example
`/run/secrets/pushover_token`) and mount them as secrets. A direct value
always wins over its `_FILE` twin.

## 3. Configuration reference

All settings are environment variables. The compose file lists every one
with its default; blank means "use the default".

Each appliance has its own block, `WASHER_*` and `DRYER_*`. An appliance is
enabled when its `_IP` is set; `WASHER_ENABLED` / `DRYER_ENABLED` override
that (`false` keeps a configured appliance switched off, handy when another
client is already talking to it). At least one must be enabled.

| Variable         | Default | Meaning                                                   |
|------------------|---------|-----------------------------------------------------------|
| `WASHER_ENABLED` / `DRYER_ENABLED` | on if `_IP` set | `true` / `false`               |
| `WASHER_IP` / `DRYER_IP` | – | LAN IP of the appliance                                 |
| `WASHER_PORT` / `DRYER_PORT` | auto | Fixed OCF port. Blank probes 5684 + 49152–49160   |
| `WASHER_NAME` / `DRYER_NAME` | Washing machine / Tumble dryer | Notification title  |
| `WASHER_COURSE_NAMES` / `DRYER_COURSE_NAMES` | – | `1C=Eco 40-60,1B=Cotton` (see [Course names](#course-names)) |
| `WASHER_DTLS_LOCAL_PORT` / `DRYER_DTLS_LOCAL_PORT` | 49700 / 49701 | Fixed local UDP port per appliance; `random` to let the OS pick |
| `PUSHOVER_TOKEN` | required | Pushover application API token (or `PUSHOVER_TOKEN_FILE`) |
| `PUSHOVER_USER`  | required | Pushover user or group key (or `PUSHOVER_USER_FILE`)     |
| `PUSHOVER_DEVICE` | all    | Deliver to one named device                                |
| `PUSHOVER_SOUND` | app default | Sound for every message                                |
| `PUSHOVER_PRIORITY` | 0    | -2 … 1                                                     |
| `PUSHOVER_FINISHED_SOUND` | – | Sound used only for "laundry is done"                  |
| `PUSHOVER_ALARM_PRIORITY` | 1 | Priority for alarm messages (exempt from quiet hours)   |
| `EVENTS`         | see below | Comma-separated event list, or `all`                     |
| `STARTUP_NOTIFY` | false   | Shorthand for adding `startup` to `EVENTS`                  |
| `EVENT_PRIORITIES` | –     | Per-event overrides, e.g. `cycle_finished=1,phase_changed=-1` |
| `QUIET_HOURS`    | –       | `22:00-07:00`: cap non-alarm priority inside this window (local time, see `TZ`) |
| `QUIET_PRIORITY` | -1      | The cap applied during quiet hours                          |
| `ALARM_REPEAT_S` | 600     | Same alarm re-sent at most once per window; max 3 alarms per window; `0` disables |
| `OFFLINE_AFTER_S` | 900    | Seconds unreachable before an `offline` event (minimum 60)  |
| `TZ`             | UTC     | Time zone for Estimated Finish and Raised times, `QUIET_HOURS`, and log timestamps |
| `LOG_LEVEL`      | INFO    | DEBUG, INFO, WARNING or ERROR                               |
| `CERT_PATH` / `KEY_PATH` | /config/client_fullchain.pem, /config/client.key | Cert location inside the container |
| `STATE_DIR`      | /data (image) | Where cycle trackers persist; blank disables            |
| `HEARTBEAT_PATH` | /tmp/smartthings-pushover.heartbeat | File the health check inspects |
| `HEALTH_INTERVAL_S` | 300  | Health log cadence (minimum 30)                             |
| `HOT_POLL_S` / `HOT_POLL_ACTIVE_S` | 2 / 1 | State poll interval idle / during a cycle (minimum 0.5) |
| `PING_INTERVAL_S` | 25     | DTLS keepalive interval (minimum 5)                         |

### Events

`EVENTS` is a comma-separated list. Default:
`cycle_scheduled,cycle_started,cycle_finished,cycle_paused,cycle_cancelled,alarm`

Every notification is titled with the appliance's name and consists of
`Label: value` lines, labels in bold. Fields that don't apply are omitted.
Durations read like `1h 12m`; clock times are `HH:MM`, 24-hour, in the
container's `TZ`. For example, a washer phase change:

```
Status: Spinning
Time Remaining: 14m
Estimated Finish: 14:43
Percentage Complete: 31%
```

and a cycle start:

```
Status: Started
Programme: Eco 40-60
Temperature: 40°
Spin: 1400 rpm
Rinses: 2
Time Remaining: 2h 36m
Estimated Finish: 16:47
```

| Event             | Fires when                                                   | Fields |
|-------------------|--------------------------------------------------------------|--------|
| `cycle_scheduled` | Start pressed with Delay End armed                           | Status, Programme, settings, Finishes In, Estimated Finish |
| `cycle_started`   | The drum starts (or the Delay End wait elapses)              | Status, Programme, Temperature / Spin / Rinses (washer) or Dry Level / Dry Time (dryer), Time Remaining, Estimated Finish |
| `cycle_finished`  | The cycle completes                                          | Status: Complete, Programme, Duration (or Cycle Length if the start wasn't seen) |
| `cycle_paused`    | Paused mid-cycle                                             | Status, Phase, Time Remaining (no finish estimate while paused) |
| `cycle_resumed`   | Resumed after a pause                                        | Status, Phase, Time Remaining, Estimated Finish |
| `cycle_cancelled` | Stopped or powered off before finishing (also a cancelled Delay End) | Status, Programme, Phase, Time Remaining |
| `phase_changed`   | Wash → Rinse → Spin (washer), Drying → Cooling (dryer)        | Status, Time Remaining, Estimated Finish, Percentage Complete |
| `alarm`           | The appliance raises an error                                | Status: Error, Code, Meaning (4C water supply, 5C drain, DC door, …), Raised (local time); raw Details when the code is unknown |
| `power_on` / `power_off` | Panel power toggled                                   | Status |
| `remote_control`  | Remote-control (SmartThings) toggle on the panel changed      | Remote Control: Enabled / Disabled |
| `child_lock`      | Child lock changed                                           | Child Lock: On / Off |
| `offline` / `online` | Appliance unreachable for `OFFLINE_AFTER_S` seconds / back | Status, Unreachable For |
| `startup`         | One message per appliance when the bridge first connects      | Status, Appliance State |

`EVENTS=all` enables everything. Anything not in the list is still logged,
just not sent.

Priorities resolve in this order: the event's own hint, then
`PUSHOVER_ALARM_PRIORITY` for alarms, then an `EVENT_PRIORITIES` override,
then the `QUIET_HOURS` cap (alarms are never capped). Alarm notifications
are additionally rate-limited by `ALARM_REPEAT_S` so a flapping sensor
cannot page you every second; suppressed alarms are still logged.

### Course names

The appliances report the selected programme as a code such as
`Table_02_Course_1C`. They never send programme *names* (those live in the
SmartThings app, not in the appliance), so the mapping has to be supplied.
To show a name instead of "Course 1C", map the codes:

```
WASHER_COURSE_NAMES=1C=Eco 40-60,1B=Cotton,74=Drum Clean
DRYER_COURSE_NAMES=16=Cotton,27=Time Dry
```

For the two tested models the full mappings are already in
`docker-compose.yml` and `.env.example`. Washer WW80CGC04DAEEU (course
table `Table_02`):

```
WASHER_COURSE_NAMES=1C=Eco 40-60,1B=Cotton,25=Synthetics,20=Hygiene Steam,08=Rinse+Spin,74=Drum Clean,87=Downloaded,06=Bedding,7F=Wool/Delicates,65=Colours,8F=Intense Cold,96=Less Microfiber,34=Mixed Load,A0=Quick Wash 15'
```

Dryer DV80CGC0B0AEEU (course table `Table_03`; `Downloaded` ships as
Shirts until you send another cycle from the app):

```
DRYER_COURSE_NAMES=16=Cotton,18=Synthetics,1F=Mixed Load,19=Delicates,20=Iron Dry,1A=Wool,1D=Towels,1B=Bedding,1E=Outdoor,43=Downloaded,23=Quick Dry 35',27=Time Dry,25=Warm Air,24=Cool Air
```

They were derived from the firmware's per-course option table cross-checked
against the user manuals. For a different model, or to double-check a code,
use `--dump`: it lists every course code the firmware advertises, marks the
one currently selected with `*`, and shows which are still unnamed. Turn
the dial to a programme, run it, note the code, repeat for each position,
then set the variable. An appliance allows only one client at a time, so
stop the container first (or set that appliance's `_ENABLED=false` in the
running one):

```sh
docker compose stop
docker compose run --rm smartthings-pushover smartthings-pushover --dump washer
docker compose start
```

## 4. Running from source

Clone the repo, then `cp .env.example .env` and fill it in. Course names
with spaces and apostrophes are fine unquoted; the file is read with
Docker's `--env-file` rules everywhere.

**Build the image locally** with the dev compose file, which mounts
`./certs` and `./data`:

```sh
docker compose -f docker-compose.dev.yml up -d --build
```

Or plain Docker:

```sh
docker build -t smartthings-pushover .
docker run -d --name smartthings-pushover --restart unless-stopped \
  --network host \
  -v "$PWD/certs:/config:ro" \
  -v "$PWD/data:/data" \
  --env-file .env \
  smartthings-pushover
```

**Bare metal** for debugging. The project is managed with
[uv](https://docs.astral.sh/uv/):

```sh
uv sync                       # creates .venv with the locked dependency set
export CERT_PATH=certs/client_fullchain.pem KEY_PATH=certs/client.key STATE_DIR=./data
uv run smartthings-pushover --env-file .env
```

Variables already exported win over the file, which is how the `CERT_PATH`
override above takes effect. Don't `source .env` in a shell; the unquoted
values break it.

The same one-shot commands work here:

```sh
uv run smartthings-pushover --env-file .env --test-notify   # Pushover accepts the keys?
uv run smartthings-pushover --env-file .env --dump          # read every enabled appliance once
uv run smartthings-pushover --env-file .env --dump dryer    # just one
uv run smartthings-pushover --env-file .env --healthcheck   # what the Docker HEALTHCHECK runs
```

If a container is already talking to one appliance, add
`WASHER_ENABLED=false` (or `DRYER_ENABLED=false`) so the local run leaves
it alone.

## How it works

- **Transport:** one authenticated DTLS session per appliance to its OCF
  endpoint using the client certificate, each in its own thread with its
  own fixed local UDP port. A fixed port matters: after a restart the
  appliance sees the same peer and drops its stale session instead of
  ignoring the new one for several minutes. The session follows the
  SmartThings-Local reference bridge: cache + tiered poll scheduler +
  keepalive + periodic OBSERVE refresh, all torn down and rebuilt on
  reconnect with jittered exponential backoff (1 s → 30 s). A stop request
  cancels a handshake in progress, so `docker stop` is prompt.
- **Freshness:** the appliance pushes OBSERVE notifications on its
  `/<x>/vs/0` resources when a state changes, usually within a second.
  Those only flow while it can also reach Samsung's cloud, so the bridge
  polls `/operational/state/vs/0` every 2 s idle / 1 s during a cycle as
  the guaranteed path. Warm resources (power, locks, course, alarms)
  refresh every 15 s and the full tree every 5 min.
- **Events:** every cache change is flattened into an `ApplianceState`
  (`smartthings_pushover/appliance.py`; every kind shares one shape, the
  fields a kind lacks are just `None`) and diffed against the previous one
  (`smartthings_pushover/events.py`). The whole tree is loaded before the
  first diff, so restarting the bridge never re-announces a running cycle.
  The last state survives reconnects: a cycle that finishes while the
  bridge is disconnected is reported when it reconnects, and if the outage
  was longer than the time that was remaining, a Run → Ready jump is read
  as "finished" rather than "cancelled". The cycle tracker (start time,
  course) is persisted in `STATE_DIR`, so "Finished after 1h 12m" survives
  a container restart too.
- **Kinds:** everything washer- or dryer-specific (course resource, phase
  wording, env-var defaults) is one entry in
  `smartthings_pushover/kinds.py`. A further appliance on the same
  firmware family is a new row there.
- **Pushover:** messages are sent with Pushover's HTML flag so labels
  render bold; the same fields are logged as plain text. A background
  queue retries on network errors and 5xx.
  A 4xx (bad token, invalid user) is logged once and not retried. A 429
  means the application's monthly quota is used up; it is logged with the
  reset time and messages are dropped until then instead of retried.
- **Health:** the main loop touches a heartbeat file while every appliance
  thread is alive; the container `HEALTHCHECK` fails if it goes stale, so
  `restart: unless-stopped` also covers a wedged process. An unreachable
  appliance is not "unhealthy": the bridge reconnects on its own and can
  send `offline` / `online` events instead.

## Caveats and observed behaviour

- **One client per appliance.** Samsung's firmware allows a single DTLS
  session per peer. Don't run this alongside SmartThings-Local's MQTT
  bridge or another local client against the same appliance, and don't
  run two copies of this bridge with the same appliance enabled; they
  will fight.
- **Read-only.** The bridge never writes to an appliance and never touches
  `/oic/sec/*`.
- **Delay End.** While armed and waiting the washer reports `Run`, no
  phase, and both `remainingTime` and `delayEndTime` equal to the time
  until the cycle *ends*, so the actual start time is not knowable. The
  bridge sends `cycle_scheduled` then, `cycle_started` when a real phase
  begins, and "Delayed Start Cancelled" if it is stopped first. Opening
  and closing the door during the wait is silent.
- **Door open at Start.** The washer reports Ready → Pause plus a `DC`
  alarm; closing the door goes Pause → Run. The bridge treats that as the
  start, not a resume. The appliance re-raises an alarm with a fresh
  timestamp each time it re-checks, so alarm rate limiting keys on the
  error code, not the message text.
- **Cycle end.** Detected from the `End` state or the `Finish` progress
  step (the washer reports `Finish` while still in `Run`, about a minute
  before the end). A transition from Run straight to Ready is reported as
  *cancelled* unless the last known remaining time was under a minute or
  the bridge had been disconnected for at least that long.
- **Stale sweeps.** The five-minute full-tree read takes a couple of
  seconds and can land after fresher data. Changes that arrive via a sweep
  are evaluated 3 s later so the 1 s state poll can correct a stale
  snapshot first.
- **Unverified so far:** a natural dryer finish, and what `delayEndTime`
  reports once a delayed cycle actually begins. Neither affects the start
  or finish notifications, which key on the progress step.

## Development

```sh
uv sync                 # dependencies + dev tools into .venv
uv run pytest
uv run ruff check .
uv run mypy
```

CI runs the same three commands plus `uv lock --check`, on the Python
version in `.python-version`, then builds and publishes the image.
Dependencies are locked in `uv.lock`; the Dockerfile installs from it with
`--frozen`. Dependabot proposes updates for the lockfile, the SHA-pinned
GitHub Actions and the digest-pinned base images.

Tests cover the state flattening (against real `/device/0` captures of
both machines), the course-table decoding, the event detector including
Delay End, door-open and reconnect-after-outage cases, config parsing, the
Pushover client and worker, alarm decoding and throttling, tracker
persistence, and the bridge itself against a fake DTLS session (seed,
observe delivery, reconnect, stop during handshake, stale sweeps). Nothing
in the test suite talks to an appliance or to Pushover.

## Credits

Protocol work, certificate tooling and the DTLS/CoAP stack are from
[QuiteYellow/SmartThings-Local](https://github.com/QuiteYellow/SmartThings-Local)
(MIT). This project adds the appliance resource map, the event detector
and the Pushover forwarder.

Development was a collaboration between the author and Claude Fable 5.1
(Anthropic), which reviewed the codebase, implemented the fixes and
features, wrote the test suite and this README, and drove the live
verification against both machines.
