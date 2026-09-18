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

# Separate endpoint for archive uploads. Leave empty to use ARCHIVE_UPLOAD_URL
# from the environment, or the built-in upload endpoint if it is unset.
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

### YAML settings and environment variables

`—` means there is no environment equivalent. Dotted keys are nested under
`monitor_filters` in YAML. Environment variables take priority for authentication
and Telegram settings; `archive_upload_url` in YAML takes priority over
`ARCHIVE_UPLOAD_URL`.

| YAML key | Environment equivalent | Description |
| --- | --- | --- |
| `webhook_url` | — | URL receiving calculation status webhooks. |
| `auth_key` | `MPDS_MONITOR_KEY` | Authentication key included in webhook form data. |
| `poll_interval` | — | Seconds between monitor scans (default: `30`). |
| `workchain_hierarchy` | — | Map of parent, child, and grandchild process labels to monitor. |
| `archive_upload_url` | `ARCHIVE_UPLOAD_URL` | Archive upload URL; uses the built-in endpoint if both are unset. |
| `archive_key` | `MPDS_ARCHIVE_KEY` | Archive authentication token; falls back to the webhook key if both are unset. |
| `archive_bid` | — | Optional identifier sent as the archive upload's `bid` form field. |
| `archive_schema_id` | — | Optional identifier sent as the archive upload's `schema_id` form field. |
| `archive_keep` | — | Keep local archives after successful upload (default: `false`); failures always retain them. |
| `send_archive` | — | Enable archive generation and upload (default: `true`). |
| `send_archives_all_stages_ready` | — | Require all configured subcalculations to succeed before archiving (default: `false`). |
| `telegram_bot_token` | `TELEGRAM_BOT_TOKEN` | Bot token; required together with a chat ID to enable Telegram reports. |
| `telegram_chat_id` | `TELEGRAM_CHAT_ID` | Destination chat or group ID for Telegram reports. |
| `notification_time` | — | Daily report time in `HH:MM` format (default: `09:00`). |
| `notification_timezone` | — | IANA timezone for daily reports (default: `UTC`). |
| `notification_user_name` | — | Default user name shown in this monitor's Telegram reports. |
| `notification_user_names` | — | Map of AiiDA owner emails to names or Telegram usernames. |
| `running_alert_hours` | — | Running duration limit in hours; `null` disables long-running alerts, while statistics still send. |
| `log_file` | — | Path to the monitor's log file. |
| `log_level` | — | YAML logging level; the daemon CLI overrides it with `--logging-level` (default: `ERROR`). |
| `log_max_bytes` | — | Log rotation size in bytes (default: `10485760`). |
| `log_backup_count` | — | Number of rotated log files to retain (default: `3`). |
| `monitor_filters` | — | Optional filters for automatic MPDS processing; Telegram reports ignore them. |
| `monitor_filters.created_after` | — | Inclusive earliest parent creation date or ISO 8601 timestamp. |
| `monitor_filters.created_before` | — | Inclusive latest parent creation date or ISO 8601 timestamp. |
| `monitor_filters.max_age_hours` | — | Scan parents no older than this many hours; combines with `created_after` using the stricter bound. |
| `monitor_filters.element_counts` | — | Allowed distinct element counts in workflow label formulas, such as `[3, 4]`. |
| `monitor_filters.element_count_greater_than` | — | Exclusive lower bound on distinct element counts. |
| `monitor_filters.compounds` | — | Exact formulas to match at the beginning of workflow labels. |
| `monitor_filters.elements` | — | Element symbols that workflow label formulas must contain. |
| `monitor_filters.elements_match` | — | Match `any` (default) or `all` configured element symbols. |

For webhook authentication, resolution is `MPDS_MONITOR_KEY`, then `auth_key`,
then `security_key`. Archive authentication uses `MPDS_ARCHIVE_KEY`, then
`archive_key`, then that webhook authentication key. Telegram settings use the
environment variable, then the lowercase YAML key, then its uppercase YAML alias.

Restart an already running monitor after changing YAML settings or environment
variables. For environment changes, start it from a shell or service with the new
values. If the upload server reports `Token has expired`, replace the archive
token with a valid one before restarting.

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

## Telegram reports in a shared chat

The monitor sends **RUNNING calculations that exceed `running_alert_hours`** to
one shared Telegram group, followed by a separate statistics message. Each
calculation entry identifies its owner, label, PK, execution hostname when
available, and elapsed running time. Statistics include all RUNNING processes
selected by this monitor, including those within the limit.

Reports are checked after the first successful scan at daemon startup and once
a day at the configured time. If no calculations are over the limit, the bot
sends the statistics message alone, including when RUNNING is zero. The monitor only
sends messages; it does
not handle commands or buttons such as `/running`, or send completion/failure
alerts when a process reaches a terminal state.

### Setup

1. Open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, and
   follow the prompts. Save the token it returns. See Telegram's
   [bot setup guide](https://core.telegram.org/bots/tutorial).
2. Add the bot to your shared group and allow it to send messages. Send a command
   addressed to it, such as `/start@YourBotUsername`, to make the group visible
   in the next step. This command is only for discovering the group ID.
3. Call the HTTPS Bot API method `getUpdates` using your token and read
   `result[].message.chat.id` from the reply. Group IDs may be negative.
   See [getUpdates](https://core.telegram.org/bots/api#getupdates).
4. Set both variables in the environment of the daemon's service:

```bash
export TELEGRAM_BOT_TOKEN="<token-from-BotFather>"
export TELEGRAM_CHAT_ID="<shared-group-chat-id>"
```

5. Set a positive `running_alert_hours` in
   `~/.aiida/aiida_mpds_monitor/conf.yaml` to enable long-running calculation alerts.
   Statistics are enabled by the credentials even when the limit is `null`.
   You can also put the credentials in
   this file instead of using environment variables:

```yaml
telegram_bot_token: "<token-from-BotFather>"
telegram_chat_id: "<shared-group-chat-id>"
running_alert_hours: 24
notification_time: "09:00"       # Daily report time, HH:MM (24-hour clock)
notification_timezone: "UTC"    # IANA timezone, e.g. Europe/Berlin
notification_user_names:
  "alice@example.org": "@alice"
  "bob@example.org": "Bob Smith (@bob)"
```

6. Start or restart the daemon with the configured service environment:

```bash
aiida-mpds-monitor --logging-level INFO
```

All users use the same bot token and group chat ID. Separate monitor instances
for separate AiiDA profiles may share these credentials: the monitor only sends
messages and does not consume Telegram updates. Run one monitor per AiiDA
profile to avoid duplicate reports for the same calculations.

### Identifying calculation owners

Keys in `notification_user_names` are the email addresses of the calculations'
AiiDA owners (`node.user.email`). Values are the names to display in the shared
chat; use `@username` for a Telegram username. Use `verdi user list` in the
monitored profile to find the owner email addresses.

For a monitor whose calculations belong to one person, you can set a default
Telegram username instead:

```yaml
notification_user_name: "@alice"
```

The singular `notification_user_name` setting also accepts a bare username such
as `"alice"` and adds `@` automatically. It applies to **every unmapped owner**
in this monitor. Entries in the plural `notification_user_names` mapping take
priority; keep using that mapping for profiles with multiple owners. Mapped
values are used verbatim, so include `@` when specifying a Telegram username.
Use the actual Telegram username, not just a profile display name. Restart the
daemon after changing the configuration.

If neither a mapping nor a default name is set, the report uses the owner's AiiDA
first and last name, falling back to their email address. When several people submit under the
same AiiDA account, the monitor sees one owner; the mapping cannot distinguish
those people without separate ownership information.

The uppercase YAML keys `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are also
accepted. For each setting, a nonempty environment value takes priority, followed
by the lowercase YAML key, then the uppercase YAML key. Restart the daemon after
editing the configuration. Restrict access to a file containing your token with
`chmod 600 ~/.aiida/aiida_mpds_monitor/conf.yaml` and keep it out of version control.

If neither setting is provided, notifications stay disabled. If only one is set,
the daemon logs a startup warning (visible with `--logging-level WARNING` or
more verbose). `poll_interval` controls how often the daemon scans, and
`workchain_hierarchy` selects process types for Telegram reports:

```yaml
poll_interval: 30
running_alert_hours: 24  # null (default) disables long-running alerts; statistics still send
workchain_hierarchy:
  MPDSStructureWorkChain:
    BaseCrystalWorkChain:
      - CrystalParallelCalculation
```

### Startup and daily schedule

`notification_time` defaults to `"09:00"` and `notification_timezone` to `"UTC"`.
Quote the time in YAML. Reports use the first successful calculation scan at or
after the scheduled local time, so the polling interval and time spent processing
webhooks or archives can delay delivery. Failed scans do not send partial or
stale reports; a later successful scan can deliver the due report.

The startup check happens after the first successful scan. A startup before the
scheduled time allows another report at that day's scheduled time. A startup
at or after the scheduled time counts as that day's daily check, avoiding two
immediate reports. Restarting the daemon intentionally performs another startup
check.

Each daily check runs once per local calendar date, even when daylight saving
time repeats an hour. If the scheduled time is skipped by a clock change, the
first successful scan afterward performs the check. A check with no overdue
calculations sends statistics and consumes the day's slot: calculations crossing
the limit later wait until the next report.

`running_alert_hours` must be positive; fractional values such as `0.5` are
supported. Only durations **strictly greater** than the limit qualify. A missing
or invalid limit disables long-running alerts; statistics remain enabled. An invalid
limit logs a warning. Invalid time or timezone
settings also disable reports with a warning. Restart the daemon after changing
these settings.

Daily checks are remembered in memory. The next scheduled check or startup can
report calculations that are still overdue; crossing the limit does not trigger
an immediate message during a polling cycle.

### Included calculations

Reports cover running processes listed at any level of `workchain_hierarchy`
in the monitor's AiiDA profile, including calculations executing remotely.
Queued, completed, and unlisted processes are excluded. Telegram reports ignore
`monitor_filters`.

Calculations exceeding `running_alert_hours` appear in the detailed report.
Each entry shows the owner, calculation name, PK, running duration, and hostname
when available. Empty labels appear as `(label not set)`.

For example:

```text
🚨 LONG-RUNNING CALCULATION 🚨
Configured limit exceeded: 24 h

User: @alice
Name: BaMnO3/185: Geometry optimization
PK: 123456
Hostname: compute-17
Running time: at least 25 h 17 min
```

At startup and each daily report time, the bot sends statistics after any overdue
calculation details:

```text
📊 Calculation statistics (this monitor)
User: @alice
Allocated servers (YaScheduler): 5
RUNNING: 12
Running longer than 24 h: 3
```

Statistics count all running processes selected by this monitor, including those
within the time limit. They still send when no calculations are running or
long-running alerts are disabled. Unknown durations count toward RUNNING but
cannot count as overdue. The `User` line appears when `notification_user_name`
is configured. Counts are not combined across monitors sharing a chat.

If an archive upload fails, the next scheduled statistics message includes a
one-time notice with the HTTP status and server explanation, for example:

```text
⚠️ Archive upload errors
Archive upload failed (HTTP 401): Token has expired
```

`Allocated servers (YaScheduler)` counts enabled servers, both busy and idle.
YaScheduler and its database dependency (`pg8000`) must be installed and
configured in the monitor's environment. If the count cannot be retrieved, it
shows `unavailable`; calculation reports still send. Monitors using the same
YaScheduler database report the same server inventory, so do not add their
server counts together.

Running duration uses the scheduler's start time when available. Otherwise, it
starts when the monitor first observes the calculation running. Reports show
**at least …** because this may be a lower bound. Normal restarts retain the
fallback timer; `--no-commit` loses it on restart. Calculations with an unknown
duration are omitted from the overdue details.

### Delivery and failures

Long reports are split into plain-text chunks of at most 2,000 characters;
splitting can occur within a calculation entry. The final statistics follow the
detail chunks. Send attempts from one monitor, including statistics and retries,
are spaced at least 3.1 seconds apart to stay below Telegram's
[group sending limit](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).
Separate instances sharing the bot do not coordinate their send timing.

For an explicit rate-limit rejection (HTTP status or API `error_code` 429), the
monitor retries the rejected chunk up to twice. It accepts an integer
`retry_after` from 0 through 60 seconds and waits at least 3.1 seconds before
retrying. A missing, invalid, or larger delay ends that chunk's attempt with an
ERROR; the monitor does not shorten a longer requested wait to 60 seconds.

Other HTTP/network failures, invalid responses, and API rejections log an ERROR
without a retry. A timeout may occur after Telegram has accepted the message.
The monitor attempts subsequent chunks and the final statistics even if an
earlier chunk fails, so receiving statistics does not confirm delivery of all
details. The scheduled slot is consumed regardless of delivery success; failed
messages do not trigger another attempt on each poll. Errors are visible at the
daemon's default WARNING logging level, and notification error messages omit
the bot token and API response descriptions.

Sending and retry waits run in the daemon loop before MPDS processing, so a
large report can delay the next webhook/archive scan. Notification failures do
not stop MPDS monitoring; each HTTP request uses a 10-second timeout.

`--dry-run` sends no Telegram requests and writes no tracking extras.
The one-shot `aiida-mpds-submit` command does not send Telegram messages.
`--resend-all` replays eligible MPDS deliveries and does not override the Telegram
schedule. `--no-commit` keeps fallback running timers in memory; restarting in
this mode loses those timers unless the scheduler supplies a start timestamp.

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

Copyright © 2026 Materials Platform for Data Science OÜ
