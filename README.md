# smartthings-pushover

A small Docker container that watches a Samsung washing machine and/or
tumble dryer on your LAN and sends Pushover notifications when a cycle
starts, pauses, finishes, is cancelled, or the machine raises an alarm. No
SmartThings cloud, no MQTT broker, no Home Assistant. One process, one DTLS
session per appliance, outbound HTTPS to Pushover.

It builds on the [smartthings-local](https://github.com/QuiteYellow/SmartThings-Local)
library (CoAP over DTLS, OBSERVE + tiered polling) and was written against a
**WW80CGC04DAEEU** washer and a **DV80CGC0B0AEEU** dryer (both
`DA_WM_TP1_21_COMMON` firmware, port 49154). Any laundry appliance on the
same firmware family that accepts the AC14K_M-derived client cert should
work. Both appliances accept the same certificate.

## 1. Mint a client certificate (once)

Follow SmartThings-Local's Part 2. From that repo:

```sh
TARGET_IP=192.168.1.252 python setup_cert.py --test
```

You need two files from its `certs/` directory:

- `client_fullchain.pem`
- `client.key`

Copy them into `./certs/` here (git-ignored) or anywhere you can mount into
the container.

## 2. Deploy on the NAS (pull the published image)

Every push to `main` builds a multi-arch image (amd64 + arm64) and pushes it
to GitHub Container Registry as
`ghcr.io/ck3mp/smartthings-pushover:latest` (see
[.github/workflows/publish.yml](.github/workflows/publish.yml)). Tags like
`v0.3.0` also publish `:0.3.0` and `:0.3`. The image contains no
certificates or secrets; everything comes from the mounted cert directory
and environment variables. It runs as an unprivileged user (uid 1000) and
has a `HEALTHCHECK`, so `restart: unless-stopped` also covers a wedged
process.

On the NAS:

1. Copy the two cert files to a directory, e.g. `/volume1/docker/smartthings-pushover/certs/`.
2. Create a writable state directory, e.g. `/volume1/docker/smartthings-pushover/data/`
   (owned by uid 1000). This keeps the per-appliance cycle tracker so a
   restart mid-cycle still produces "Finished after 1h 12m".
3. Copy [docker-compose.yml](docker-compose.yml) next to them and edit the
   `environment:` block (at minimum `WASHER_IP` and/or `DRYER_IP`,
   `PUSHOVER_TOKEN`, `PUSHOVER_USER`) and the two volume paths.
4. Because the repo is private, the package is private too. Log in once
   with a GitHub personal access token that has the `read:packages` scope:

   ```sh
   docker login ghcr.io -u ck3mp
   ```

5. Start it:

   ```sh
   docker compose pull
   docker compose up -d
   docker compose logs -f
   ```

Upgrade later with `docker compose pull && docker compose up -d`.

### Configuration reference

All settings are environment variables. The compose file lists every one
with its default; blank means "use the default".

Each appliance has its own block, `WASHER_*` and `DRYER_*`. An appliance is
enabled when its `_IP` is set; `WASHER_ENABLED` / `DRYER_ENABLED` override
that (`false` keeps a configured appliance switched off, handy when another
bridge is already talking to it). At least one must be enabled.

| Variable         | Default | Meaning                                                   |
|------------------|---------|-----------------------------------------------------------|
| `WASHER_ENABLED` / `DRYER_ENABLED` | on if `_IP` set | `true` / `false`               |
| `WASHER_IP` / `DRYER_IP` | – | LAN IP of the appliance (give it a DHCP reservation)   |
| `WASHER_PORT` / `DRYER_PORT` | auto | Fixed OCF port. Blank probes 5684 + 49152–49160   |
| `WASHER_NAME` / `DRYER_NAME` | Washing machine / Tumble dryer | Notification title  |
| `WASHER_COURSE_NAMES` / `DRYER_COURSE_NAMES` | – | `1C=Eco 40-60,1B=Cotton` (see below) |
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
| `CERT_PATH` / `KEY_PATH` | /config/client_fullchain.pem, /config/client.key | Cert location inside the container |
| `STATE_DIR`      | /data (image) | Where cycle trackers persist; blank disables            |
| `HEARTBEAT_PATH` | /tmp/smartthings-pushover.heartbeat | File the health check inspects |
| `TZ`             | UTC     | Time zone for `QUIET_HOURS` and log timestamps              |
| `LOG_LEVEL`      | INFO    | DEBUG, INFO, WARNING or ERROR                               |
| `HEALTH_INTERVAL_S` | 300  | Health log cadence (minimum 30)                             |
| `HOT_POLL_S` / `HOT_POLL_ACTIVE_S` | 2 / 1 | State poll interval idle / during a cycle (minimum 0.5) |
| `PING_INTERVAL_S` | 25     | DTLS keepalive interval (minimum 5)                         |

The pre-dryer names `COURSE_NAMES` and `DTLS_LOCAL_PORT` are still read
as the washer's values when the `WASHER_*` variant is not set.

Secrets can come from files instead of the environment: set
`PUSHOVER_TOKEN_FILE=/run/secrets/pushover_token` (and the `_USER_FILE`
twin) and mount Docker secrets. A direct value always wins over its file.

### Events

`EVENTS` is a comma-separated list. Default:
`cycle_scheduled,cycle_started,cycle_finished,cycle_paused,cycle_cancelled,alarm`

| Event             | Fires when                                                     |
|-------------------|----------------------------------------------------------------|
| `cycle_scheduled` | Delay End armed and Start pressed: the machine reports Run with no phase and a non-zero `delayEndTime`. Says when it will finish |
| `cycle_started`   | Ready/End → Run (or the Delay End wait elapses). Message includes course, temp/spin/rinses (washer) or dry level/time (dryer), ETA |
| `cycle_finished`  | Run/Pause → End, or progress reaches Finish. "Laundry is done" / "Laundry is dry", with elapsed time |
| `cycle_paused`    | Run → Pause                                                    |
| `cycle_resumed`   | Pause → Run                                                    |
| `cycle_cancelled` | Run/Pause → Ready without finishing (also a cancelled Delay End) |
| `phase_changed`   | Wash → Rinse → Spin (washer), Drying → Cooling (dryer) while running |
| `alarm`           | `/alarms/vs/0` becomes non-empty. Known error codes are decoded (4C water supply, 5C drain, dC door, …) and the raw fields are appended |
| `power_on` / `power_off` | Panel power toggled                                     |
| `remote_control`  | Remote-control (SmartThings) toggle on the panel changed        |
| `child_lock`      | Child lock changed                                             |
| `offline` / `online` | Appliance unreachable for `OFFLINE_AFTER_S` seconds / back   |
| `startup`         | One message per appliance when the bridge first connects        |

`EVENTS=all` enables everything. Anything not in the list is still logged,
just not sent.

Priorities resolve in this order: the event's own hint, then
`PUSHOVER_ALARM_PRIORITY` for alarms, then an `EVENT_PRIORITIES` override,
then the `QUIET_HOURS` cap (alarms are never capped). Alarm notifications
are additionally rate-limited by `ALARM_REPEAT_S` so a flapping sensor
cannot page you every second; suppressed alarms are still logged.

### Course names

The appliances report the selected programme as a code such as
`Table_02_Course_1C`. They never send programme *names* - those live in
the SmartThings app, not in the appliance - so the mapping has to be
built by hand. To show a name instead of "Course 1C", map the codes:

```
WASHER_COURSE_NAMES=1C=Eco 40-60,1B=Cotton,74=Drum Clean
DRYER_COURSE_NAMES=16=Cotton,27=Time Dry
```

`--dump` (below) lists every course code the firmware advertises, marks
the one currently selected with `*`, and shows which codes are still
unnamed. Turn the dial to a programme, run `--dump washer` (or `dryer`),
note the code, repeat for each position, then set the variable. An
appliance only allows one DTLS client at a time, so stop the bridge
container (or set that appliance's `_ENABLED=false` in the running one)
while you do this.

For the two machines this was written against, the mappings were derived
from the firmware's per-course option table (allowed temperatures, spin,
rinse, dry level, dry time, and option availability flags) cross-checked
against the user manuals. They should be right, but confirm a code by
dialing it. Washer WW80CGC04DAEEU (course table `Table_02`):

```
WASHER_COURSE_NAMES=1C=Eco 40-60,1B=Cotton,25=Synthetics,20=Hygiene Steam,08=Rinse+Spin,74=Drum Clean,87=Downloaded,06=Bedding,7F=Wool/Delicates,65=Colours,8F=Intense Cold,96=Less Microfiber,34=Mixed Load,A0=Quick Wash 15'
```

Dryer DV80CGC0B0AEEU (course table `Table_03`; `Downloaded` ships as
Shirts until you send another cycle from the app):

```
DRYER_COURSE_NAMES=16=Cotton,18=Synthetics,1F=Mixed Load,19=Delicates,20=Iron Dry,1A=Wool,1D=Towels,1B=Bedding,1E=Outdoor,43=Downloaded,23=Quick Dry 35',27=Time Dry,25=Warm Air,24=Cool Air
```

## 3. Building locally instead

[docker-compose.dev.yml](docker-compose.dev.yml) builds from source and
reads settings from a `.env` file (`cp .env.example .env`):

```sh
docker compose -f docker-compose.dev.yml up -d --build
```

Or plain Docker:

```sh
docker build -t smartthings-pushover .
docker run -d --name smartthings-pushover --restart unless-stopped \
  --network host \
  -v /path/to/certs:/config:ro \
  -v /path/to/data:/data \
  --env-file .env \
  smartthings-pushover
```

Bare metal for debugging (the project is managed with [uv](https://docs.astral.sh/uv/)):

```sh
uv sync                       # creates .venv with the locked dependency set
export CERT_PATH=certs/client_fullchain.pem KEY_PATH=certs/client.key STATE_DIR=./data
uv run smartthings-pushover --env-file .env
```

`--env-file` reads the file with the same rules as `docker run --env-file`
(value is everything after `=`, verbatim), so course names with spaces and
apostrophes work. Variables already exported win over the file, which is
how the `CERT_PATH` override above takes effect. Don't `source .env` in a
shell: the unquoted values break it.

## Checking things work

```sh
# Does Pushover accept the token/user?
uv run smartthings-pushover --env-file .env --test-notify

# Can we reach the appliances? Prints each one's flattened state, current
# course code, and every course code it advertises (for *_COURSE_NAMES).
uv run smartthings-pushover --env-file .env --dump          # every enabled appliance
uv run smartthings-pushover --env-file .env --dump dryer    # just one

# Is a running bridge healthy? (what the Docker HEALTHCHECK runs)
uv run smartthings-pushover --env-file .env --healthcheck
```

When running outside the container, export the bare-metal cert paths first
(`CERT_PATH=certs/client_fullchain.pem KEY_PATH=certs/client.key`). If the
deployed container is already talking to one appliance, add
`WASHER_ENABLED=false` (or `DRYER_ENABLED=false`) so the local run leaves
it alone.

Inside the container: `docker compose run --rm smartthings-pushover smartthings-pushover --dump`.
The test-notify variant works the same way.

Expected first-run log:

```
INFO    main      smartthings-pushover 0.3.0 -> Pushover; events=alarm,cycle_cancelled,cycle_finished,cycle_paused,cycle_scheduled,cycle_started
INFO    main      washer: Washing machine @ 192.168.1.252:auto
INFO    main      dryer: Tumble dryer @ 192.168.1.253:auto
INFO    washer    discovered DTLS port 49154
INFO    dryer     discovered DTLS port 49154
INFO    washer    DTLS connected to 192.168.1.252:49154
INFO    dryer     DTLS connected to 192.168.1.253:49154
INFO    washer    identified DA_WM_TP1_21_COMMON_WW5000C/DC92-03495A_906C serial=…
INFO    dryer     identified DA_WM_TP1_21_COMMON_DV5000C/DC92-03499A_150C serial=…
INFO    washer    seeded 33 resources; state=Ready progress=None remaining=02:36:00
INFO    dryer     seeded 30 resources; state=Ready progress=None remaining=03:10:00
```

A health line per appliance is logged every `HEALTH_INTERVAL_S` (default
300 s) with connection, poll and notification counters.

## How it works

- **Transport:** one authenticated DTLS session per appliance to its OCF
  endpoint using the client certificate, each in its own thread with its
  own fixed local port. Same session model as the SmartThings-Local
  reference bridge: cache + tiered poll scheduler + keepalive + periodic
  OBSERVE refresh, all torn down and rebuilt on reconnect with jittered
  exponential backoff (1 s → 30 s). A stop request cancels a handshake in
  progress, so `docker stop` is prompt.
- **Freshness:** the appliance pushes OBSERVE notifications on its
  `/<x>/vs/0` resources when a state changes, usually within ~100 ms. Those
  only flow while it can also reach Samsung's cloud, so the bridge
  polls `/operational/state/vs/0` every 2 s idle / 1 s during a cycle as the
  guaranteed path. Warm resources (power, locks, course, alarms) refresh
  every 15 s and the full tree every 5 min.
- **Events:** every cache change is flattened into an `ApplianceState`
  (`smartthings_pushover/appliance.py`; every kind shares one shape, the
  fields a kind lacks are just `None`) and diffed against the previous one
  (`smartthings_pushover/events.py`). A seed loads the whole tree before
  the first diff, so restarting the bridge never re-announces a cycle. The
  last state survives reconnects: a cycle that finishes while the bridge
  is disconnected is reported when it reconnects, and if the outage was
  longer than the time that was remaining, a Run → Ready jump is read as
  "finished" rather than "cancelled".
- **Kinds:** everything washer- or dryer-specific (course resource, "done"
  wording, phase verbs, env-var defaults) is one entry in
  `smartthings_pushover/kinds.py`. A further appliance on the same
  firmware family is a new row there.
- **Pushover:** a background queue with retries on network errors and 5xx.
  A 4xx (bad token, invalid user) is logged once and not retried. A 429
  means the application's monthly quota is used up; it is logged with the
  reset time and messages are dropped until then instead of retried.

## Caveats

- **One client per appliance.** Samsung's RT-OCF allows a single DTLS
  session per peer. Don't run this alongside SmartThings-Local's MQTT
  bridge or localthings against the same appliance, and don't run two
  copies of this bridge with the same appliance enabled; they will fight.
- **Read-only.** The bridge never writes to an appliance and never touches
  `/oic/sec/*`.
- Alarms were captured once, with the door open at Start: `/alarms/vs/0`
  carries an `items` list whose entries have `code=ErrorCode_DC`, a UTC
  `triggeredTime`, `alarmType=Device` and `state=Created`. The appliance
  re-raises the same alarm with a fresh `triggeredTime` each time it
  re-checks, so rate limiting keys on the code, not the text. Codes are
  decoded via the manual's error-code table and the raw fields are printed
  after the decoded line; the raw representation is also logged at INFO.
- Delay End was captured on the WW80: while armed and waiting the machine
  reports `Run`, progress `None`, and both `remainingTime` and
  `delayEndTime` equal to the time until the cycle *ends* (so the actual
  start time is not knowable). The bridge sends `cycle_scheduled` then,
  `cycle_started` when a real phase begins, and "Delayed start cancelled"
  if it is stopped first. A door opened and closed during the wait is
  silent.
- Pressing Start with the door open reports `Ready -> Pause` plus a `DC`
  alarm; closing the door then goes `Pause -> Run`. The bridge treats that
  as the start, not a resume.
- A full-tree sweep takes a couple of seconds and can land after fresher
  data. Changes that arrive via a sweep are evaluated 3 s later so the
  1 s state poll can correct a stale snapshot first.
- Cycle end detection uses the `End` state and the `Finish` progress step.
  A transition from Run straight to Ready is reported as *cancelled* unless
  the last known remaining time was under a minute or the bridge had been
  disconnected for at least that long.

## Development

```sh
uv sync                 # dependencies + dev tools into .venv
uv run pytest
uv run ruff check .
uv run mypy
```

CI runs the same three commands plus `uv lock --check`, on the Python
version in `.python-version`. Dependencies are locked in `uv.lock`; the
Dockerfile installs from it with `--frozen`. Dependabot proposes updates
for the lockfile, the SHA-pinned GitHub Actions and the digest-pinned base
images.

Tests cover the state flattening (against real `/device/0` captures of
both machines), the course-table decoding, the event detector including
Delay End and reconnect-after-outage cases, config parsing, the Pushover
client and worker, alarm decoding and throttling, tracker persistence,
and the bridge itself against a fake DTLS session (seed, observe
delivery, reconnect, stop during handshake). Nothing in the test suite
talks to an appliance or to Pushover.

## Credits

Protocol work, certificate tooling and the DTLS/CoAP stack are from
[QuiteYellow/SmartThings-Local](https://github.com/QuiteYellow/SmartThings-Local)
(MIT). This project only adds the appliance resource map, the event
detector and the Pushover forwarder.
