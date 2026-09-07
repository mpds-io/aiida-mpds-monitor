# aiida-mpds-monitor

A daemon and CLI tool that monitors AiiDA workflows and sends webhooks when configured child workchains complete. Built for the MPDS backend: when a calculation finishes or fails, the tool posts a status payload to a configured endpoint and uploads a `7z` archive of retrieved outputs. The workchain hierarchy and endpoints live in a YAML config file, so you can monitor new workchain types without editing code.

## Installation

```bash
git clone https://github.com/mpds-io/aiida-mpds-monitor.git
cd aiida-mpds-monitor
pip install .
```

## Workflow Label Requirement

> [!IMPORTANT]
> The **label** field on AiiDA workflows determines webhook delivery and server-side processing. The monitor uses it to identify which object the workflow belongs to and which task it solved. Workflows without a label are skipped.
> Give each workflow a descriptive label, e.g. `HgI2/137: Geometry optimization`.

## Configuration

On first run the tool writes a default config file at
`~/.aiida/aiida_mpds_monitor/conf.yaml`.

```yaml
webhook_url: "http://localhost:8080"

# Separate endpoint for archive uploads. Leave empty to derive from
# webhook_url + "/upload/absolidix" (deprecated, will log a warning).
archive_upload_url: "http://localhost:8080"

# Keep .7z archives on disk after upload? (default: false — delete on success)
archive_keep: false

# Auth key for archive uploads. Leave empty to fall back to MPDS_MONITOR_KEY.
# Can also be set via the MPDS_ARCHIVE_KEY environment variable.
archive_key: ""

# Disable archive generation and upload entirely (default: true)
# send_archive: false
# send_archives_all_stages_ready: false

poll_interval: 60

# Optional automatic-monitor filters. Empty values disable each filter.
monitor_filters:
  # Parent creation dates in simple YYYY-MM-DD format. The complete first and
  # last days are included. Full ISO 8601 timestamps are also accepted.
  created_after: null
  created_before: null
  # Rolling alternative to created_after; only parents this many hours old or
  # newer are scanned. If both are set, the more restrictive bound is used.
  max_age_hours: null
  # Filter child labels by distinct elements in their leading formula.
  # Use positive integers: 2=binary, 3=ternary, and so on.
  element_counts: []
  # Optional strict lower bound. 2 accepts compounds with 3 or more elements.
  element_count_greater_than: null
  # Optional exact formulas taken from the beginning of workflow labels.
  compounds: []
  # Optional element symbols. Match at least one by default, or set
  # elements_match to "all" to require every listed element.
  elements: []
  elements_match: any

workchain_hierarchy:
  MPDSStructureWorkChain:
    BaseCrystalWorkChain:
      - CrystalParallelCalculation

log_file: "/path/to/logs/aiida_mpds_monitor.log"
log_level: "WARNING"          # DEBUG, INFO, WARNING, ERROR
log_max_bytes: 10485760       # 10 MB
log_backup_count: 5
```

## Usage

1. Configure the workchain hierarchy:

```yaml
# ~/.aiida/aiida_mpds_monitor
webhook_url: "http://example.com/webhook"
auth_key: "your-api-key"
workchain_hierarchy:
  ParentType:
    ChildType:
      - GrandchildType1
```

2. Set the auth key and start the daemon:

```bash
export MPDS_MONITOR_KEY="your-api-key"
# Optional: separate key for archive uploads
export MPDS_ARCHIVE_KEY="your-archive-key"
aiida-mpds-monitor
```

The daemon:

- Scans for parent workflows matching configured types every `poll_interval` seconds.
- Walks each parent to its children and grandchildren.
- Checks grandchild calculation status.
- Sends a webhook with the status.
- Generates a `.7z` archive and uploads it to `archive_upload_url` (skip with `send_archive: false`).
- Deletes the local archive on successful upload (unless `archive_keep: true`).
- Marks processed parents to avoid duplicates.

### Filtering monitored workflows

Filters apply to the continuously running `aiida-mpds-monitor` daemon. They do
not restrict an explicitly requested `aiida-mpds-submit PARENT_PK` operation.

For example, to monitor binary and ternary compounds created since August 1,
2026:

```yaml
monitor_filters:
  created_after: 2026-08-01
  created_before: 2026-08-31
  element_counts: [2, 3]
```

For a rolling seven-day window instead:

```yaml
monitor_filters:
  max_age_hours: 168
  element_counts: [2, 3]
```

To send only compounds with more than two distinct elements:

```yaml
monitor_filters:
  element_count_greater_than: 2
```

If `element_counts` and `element_count_greater_than` are both configured, a
compound must satisfy both filters. For example, `[2, 3, 4]` combined with a
threshold of `2` accepts only counts `3` and `4`.

To send only specific compounds:

```yaml
monitor_filters:
  compounds: [BaMnO3, HgI2]
```

To send compounds containing either barium or manganese:

```yaml
monitor_filters:
  elements: [Ba, Mn]
  elements_match: any
```

To require both elements in every compound:

```yaml
monitor_filters:
  elements: [Ba, Mn]
  elements_match: all
```

All enabled compound filters are combined with AND. Formula matching is exact
and case-sensitive: `BaMnO3` matches labels beginning with `BaMnO3`, but not
`Ba2MnO4`.

The element count is taken from the chemical formula at the beginning of the
workflow label. For example, `BaPd3P/109: Geometry optimization` is ternary
(three distinct elements), while `HgI2/137: Geometry optimization` is binary.
When `element_counts` is enabled, labels without a recognizable leading
formula are skipped and recorded in the log. Time bounds filter the parent
workchain's AiiDA `ctime`.

Options:

- `--dry-run`: Scan and log, skip sends and marks.
- `--no-commit`: Send webhooks, skip setting AiiDA extras.
- `--resend-all`: Ignore processed flags during the first scan and send every
  eligible webhook once, then continue in normal monitoring mode.
- `--logging-level` / `-l`: Set verbosity (DEBUG through CRITICAL; defaults to ERROR).

`--dry-run` and `--no-commit` are mutually exclusive; `--dry-run` wins.

Examples:

```bash
aiida-mpds-submit 12345                        # default log level (ERROR)
aiida-mpds-submit 12345 --logging-level INFO  # more verbosity
aiida-mpds-monitor --logging-level DEBUG      # debug daemon
```

3. Submit a single parent by hand (useful for backfills or debugging):

```bash
# Send webhooks for all configured children of parent PK=12345
aiida-mpds-submit 12345

# Dry-run: see what would be sent (no HTTP request)
aiida-mpds-submit 12345 --dry-run
```

If you prefer environment-based auth instead of storing the key in the settings file:

```bash
export MPDS_MONITOR_KEY="your-api-key"
aiida-mpds-submit 12345
```

## Telegram notifications

1. Open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, and
   follow the prompts. Save the token it returns. See Telegram's
   [bot setup guide](https://core.telegram.org/bots/tutorial).
2. Open your new bot's chat and send `/start`. For a group, add the bot and
   send a command addressed to it, such as `/start@YourBotUsername`.
3. Call the HTTPS Bot API method `getUpdates` using your token and read
   `result[].message.chat.id` from the reply. Group IDs may be negative.
   See [getUpdates](https://core.telegram.org/bots/api#getupdates).
4. Set both variables in the environment of the daemon's service, then restart it:

```bash
export TELEGRAM_BOT_TOKEN="<token-from-BotFather>"
export TELEGRAM_CHAT_ID="<chat-id>"
aiida-mpds-monitor --logging-level INFO
```

Keep the token in your service environment; no Telegram secrets belong in YAML.
If neither variable is set, notifications stay disabled. If only one is set,
the daemon logs a startup warning (visible with `--logging-level WARNING` or
more verbose). The existing YAML controls polling and which processes to watch:

```yaml
poll_interval: 30
running_alert_hours: 24  # null (default) disables long-running alerts
monitor_filters:
  max_age_hours: 168
workchain_hierarchy:
  MPDSStructureWorkChain:
    BaseCrystalWorkChain:
      - CrystalParallelCalculation
```

You receive terminal events for configured parents, child workchains, and
grandchild calculations: successful completion, nonzero-exit failure, killed,
and excepted. Messages include PK, process name, native state, exit status,
and computer when available. Each process reports its own outcome; a child
failure does not rewrite its parent's native state. Running/waiting processes
do not generate terminal-event messages. You can also enable long-running alerts
as described below. Daemon-error alerts are not included.

The daemon checks notifications once per loop, including parents already marked
as processed by MPDS. It applies the parent creation-time filters and child
compound filters. A parent qualifies if its label or an included child matches.
On first enabling notifications, you also receive terminal events for existing
matching nodes. Use creation-time filters to limit this initial history and the
number of nodes scanned. Each loop sleeps for `poll_interval` after its work;
HTTP requests add to the time between scans.

For duplicate prevention, the daemon records the terminal event in the AiiDA
extra `monitor_notification_state` **before** attempting delivery. Repeated polls,
restarts, and `--resend-all` do not repeat that event. Run one notifying daemon
per profile: the extras check and write are not an atomic lock between daemons.

Delivery uses the existing `requests` dependency and HTTPS
[sendMessage](https://core.telegram.org/bots/api#sendmessage) with a 10-second
timeout. The daemon logs failures and continues monitoring. It does not retry
failed attempts, because a timeout can occur after Telegram accepts a message.
A network failure or crash between recording and sending can therefore lose an
alert. This is best-effort delivery with duplicate prevention, not guaranteed
delivery.

`--dry-run` sends no notifications and writes no notification extras.
`--no-commit` deduplicates in memory only, so restarting in that mode can repeat
messages. The one-shot `aiida-mpds-submit` command does not send Telegram alerts.

### Current calculations and long-running alerts

Send `/start` to display the **Текущие расчёты** button. Press it or send
`/running` to request a report of the monitored nodes currently in RUNNING.
The report includes each node's PK, process type, AiiDA `label`, `description`,
and observed RUNNING duration. For workchains it also includes direct child
labels and descriptions, preserving the workflow's own wording. Long reports
arrive as multiple messages. Parent and child processes have separate entries
when both are RUNNING. The same hierarchy and filters used for notifications
apply to these reports.

The bot accepts requests only from the numeric chat ID in `TELEGRAM_CHAT_ID`.
In a group, any member of that configured chat can request a report. The daemon
uses [getUpdates](https://core.telegram.org/bots/api#getupdates) once per scan;
allow the scan duration plus `poll_interval` for a response. Use a bot without
an active Telegram webhook and run only one consumer of its updates. Update
offsets live in memory; a restart may repeat an unacknowledged command response.

To enable automatic alerts, set this in
`~/.aiida/aiida_mpds_monitor/conf.yaml`, then restart the daemon:

```yaml
running_alert_hours: 24
```

Positive fractional hours, such as `0.5`, are supported. Use `null` to disable
these alerts. Invalid values produce a warning and disable the threshold.
You receive one alert per observed RUNNING interval when its duration exceeds
the threshold, using the same message details and best-effort delivery policy
as terminal alerts. Changing the threshold does not repeat an alert already
attempted for that interval.

AiiDA's current process state does not supply a timestamp for entry into RUNNING.
The monitor records its first RUNNING observation in the extra
`monitor_running_interval`. The report therefore says **«не менее …»**, measured
from that observation, rather than using the node's creation or modification time.
For nodes already running when you enable this feature, the timer starts at the
first scan. Normal restarts retain the timer; `--no-commit` keeps it in memory
only. Observing any other state resets the interval. State changes between
polls or while the monitor is stopped cannot be reconstructed; durations assume
the RUNNING interval continued between observations. This measures AiiDA's
RUNNING state, not scheduler wall time or CPU usage.

## Testing with Stub Server

Start a local stub that accepts webhooks and prints them:

```bash
aiida-mpds-stub
```

Listens on `http://localhost:8080`.

## Architecture

The system uses hierarchical configuration:

1. **Parent workchains**: top-level workflows to monitor (configurable)
2. **Child workchains**: expected calculations under each parent (configurable)
3. **Grandchild checks**: validation of specific child process types (configurable)

Change the YAML config to monitor any workflow hierarchy without editing code.

## Archive Upload and Cleanup

After sending a webhook, the daemon and CLI build a `.7z` archive and upload it to a separate endpoint.

**`archive_upload_url`** sets the destination for archive uploads. Example curl equivalent:

**`archive_keep`** controls local cleanup. Default `false`: delete the `.7z` after a successful upload. Set `true` to keep archives on disk. Failed uploads retain the archive for manual recovery regardless of this setting.

**`send_archive`** controls whether the daemon and CLI generate and upload archives at all. Default `true`. Set `false` to skip archive creation and upload entirely (webhooks are still sent).

**`send_archives_all_stages_ready`** controls archive readiness. Default `false`:
a structure is included when at least one configured subcalculation succeeds.
Set `true` to include structures only after all configured subcalculations succeed.

Before creating the `.7z`, the monitor checks that the collected contents contain at least one non-empty calculation folder. With `send_archives_all_stages_ready: true`, every process below the selected base workchains must also have completed successfully (`is_finished_ok`). The number of calculation folders and their filenames are workflow-dependent. Failing an applicable check prevents archive creation and upload.

**`archive_key`** is the auth key sent with archive uploads. Resolution order: `MPDS_ARCHIVE_KEY` environment variable, then `archive_key` from config, then `MPDS_MONITOR_KEY` (the webhook key) as a fallback. This lets archive and webhook endpoints use separate credentials.

Copyright © 2026 Materials Platform for Data Science OÜ
