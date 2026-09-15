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

## Telegram reports in a shared chat

The bot sends reports containing **only RUNNING calculations that exceed
`running_alert_hours`** to one shared Telegram group. Each entry identifies the
calculation owner by their configured Telegram name.

Reports are checked after the first successful scan at daemon startup and once
a day at the configured time. If no calculations are over the limit, the bot
sends nothing. Commands and buttons, including `/running`, no longer request
reports.

1. Open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, and
   follow the prompts. Save the token it returns. See Telegram's
   [bot setup guide](https://core.telegram.org/bots/tutorial).
2. Add the bot to your shared group and allow it to send messages. Send a command
   addressed to it, such as `/start@YourBotUsername`, to make the group visible
   in the next step. This command is only for discovering the group ID.
3. Call the HTTPS Bot API method `getUpdates` using your token and read
   `result[].message.chat.id` from the reply. Group IDs may be negative.
   See [getUpdates](https://core.telegram.org/bots/api#getupdates).
4. Set both variables in the environment of the daemon's service, then restart it:

```bash
export TELEGRAM_BOT_TOKEN="<token-from-BotFather>"
export TELEGRAM_CHAT_ID="<shared-group-chat-id>"
aiida-mpds-monitor --logging-level INFO
```

Alternatively, add the settings to `~/.aiida/aiida_mpds_monitor/conf.yaml`
(the configuration filename used by this application):

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
more verbose). The existing YAML controls polling and which processes to watch:

```yaml
poll_interval: 30
running_alert_hours: 24  # null (default) disables Telegram calculation reports
monitor_filters:
  max_age_hours: 168
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
first successful scan afterward performs the check. An empty check also consumes
the day's slot: calculations crossing the limit later wait until the next report.

`running_alert_hours` must be positive; fractional values such as `0.5` are
supported. Only durations **strictly greater** than the limit qualify. A missing
or invalid limit disables reports and logs a warning. Invalid time or timezone
settings also disable reports with a warning. Restart the daemon after changing
these settings.

Daily checks are remembered in memory. A failed Telegram delivery is logged and
is not retried in every polling cycle, because a timeout may occur after Telegram
has already accepted the message. The next scheduled check or startup can report
calculations that are still overdue. Long reports are split into multiple messages.

### Included calculations

Reports select process types listed anywhere in `workchain_hierarchy`: parent
keys, child keys, and calculation labels in the lists. The daemon queries
`ProcessNode` directly for these types. It includes processes whose native AiiDA
`process_state` is `running`, and calculations whose scheduler state is
`running` while their AiiDA process remains active (`created`, `waiting`, or
`running`). AiiDA normally uses `waiting` while a CalcJob executes remotely;
the Telegram report presents this as the effective calculation state RUNNING.
Call-link depth and the existence or state of parent nodes do not restrict the
report. For example, a running `CrystalParallelCalculation` appears whenever
that type is listed in the hierarchy and its running time exceeds the limit.
Unlisted process types do not appear.
Telegram reports ignore `monitor_filters` and MPDS delivery markers; normal
MPDS webhook/archive filters remain unchanged.

Each entry contains the owner's name, the node's PK, RUNNING duration, and its own
`label.strip()`. For YaScheduler jobs, it also includes the assigned
`node.hostname` from `yastatus --json` when that field is available. A node
with an empty label remains in the report with
`(label not set)` as its name. It does not expose the internal AiiDA `waiting`
state for an executing scheduler job. Queued and terminal calculations are
excluded. The daemon must use the same AiiDA profile as the calculations you
want to inspect.

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

After the calculation details, the bot sends a separate final message with
statistics from the same completed scan:

```text
📊 Calculation statistics (this monitor)
User: @alice
RUNNING: 12
Running longer than 24 h: 3
```

The totals cover all RUNNING processes selected by this monitor's
`workchain_hierarchy`, including those within the time limit. Processes with
unknown duration count toward RUNNING but cannot count as over the limit.
Queued and terminal processes are excluded. Counts are local to this monitor;
they are not combined across machines sharing the chat. The `User` line appears
when `notification_user_name` is configured. Statistics follow the existing
startup/daily schedule and are sent only when the report contains overdue
calculations; a check with none remains silent.

AiiDA uses the scheduler's `dispatch_time` when the scheduler plugin provides it.
For YaScheduler, the monitor reads the RUNNING transition time from the task's
`updated_at` value returned by `yastatus --json`. This allows existing jobs to
show their actual elapsed execution time immediately after a monitor restart.
If the scheduler cannot provide a start timestamp, the monitor records its first
RUNNING observation in the `monitor_running_interval` extra and reports a lower
bound with **at least …**. Normal restarts retain that fallback timer;
`--no-commit` keeps it in memory only. Observing any other state resets the
interval. This measures scheduler execution time when available, not CPU usage.
Calculations with an unavailable duration are omitted from scheduled reports
because they cannot be confirmed over the limit.

`--dry-run` sends no Telegram requests and writes no tracking extras.
The one-shot `aiida-mpds-submit` command does not send Telegram messages.
Network/API failures are logged without stopping MPDS monitoring; requests use
an HTTP timeout of 10 seconds. Existing webhook and archive processing continues
normally.

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
