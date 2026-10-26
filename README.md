# aiida-mpds-monitor

**aiida-mpds-monitor** monitors AiiDA workflows and **sends calculation status webhooks and result archives to MPDS**. It provides a polling daemon and command-line tools for submitting results, repairing CRYSTAL archives, and sending Telegram reports.

## Features

- YAML configuration for parent, child, and calculation process types.
- Status webhooks for completed and failed calculations.
- `.7z` archives of retrieved outputs and submitted inputs.
- CRYSTAL archive validation and recovery after a specific server rejection.
- Workflow filters by creation date, formula, and chemical elements.
- Optional Telegram reports for running calculations and upload errors.
- Dry runs, delivery tracking, and rotating logs.

## Prerequisites

- Python 3.9 or later, as declared in the package metadata.
- Git to install the project and its Git-based dependency.
- A configured default AiiDA profile with access to the workflows and retrieved files.
- Write access to the working directory and configured log location.
- Webhook and archive upload endpoints for MPDS delivery.

The package installs `aiida-core`, `PyYAML`, `requests`, and `dft_organizer`. CRYSTAL validation uses `pycrystal` and the scientific Python dependencies supplied through `dft_organizer`.

## Installation

Install into the environment that provides your AiiDA profile:

```bash
git clone https://github.com/mpds-io/aiida-mpds-monitor.git
cd aiida-mpds-monitor
python -m pip install .
```

For an editable development installation:

```bash
python -m pip install -e '.[dev]'
```

## Quick start

Create the configuration directory:

```bash
mkdir -p ~/.aiida/aiida_mpds_monitor
```

Save this configuration as `~/.aiida/aiida_mpds_monitor/conf.yaml`. Replace the local endpoints with your server URLs before sending results.

```yaml
webhook_url: "http://localhost:8080"
archive_upload_url: "http://localhost:8080/upload"
poll_interval: 30

send_archive: true
archive_keep: false
archive_keep_errors: false
archive_errors_dir: ""  # Optional directory for preserved rejected archives.

workchain_hierarchy:
  MPDSStructureWorkChain:
    BaseCrystalWorkChain:
      - CrystalParallelCalculation

log_file: "./aiida_mpds_monitor.log"
log_max_bytes: 10485760
log_backup_count: 3
```

Set the authentication key and inspect a scan:

```bash
export MPDS_MONITOR_KEY="<webhook-key>"
aiida-mpds-monitor --dry-run --resend-all --logging-level INFO
```

Start continuous monitoring:

```bash
aiida-mpds-monitor --logging-level INFO
```

Give monitored child workchains a label such as `HgI2/137: Geometry optimization`. The monitor uses labels as webhook payloads and skips status delivery for unlabeled workchains. Compound filters also read the formula at the beginning of the label.

## Usage

### Monitor workflows

```bash
aiida-mpds-monitor --logging-level INFO
```

The daemon scans the default AiiDA profile at the configured polling interval. It checks the configured hierarchy, sends status webhooks, and uploads eligible archives. AiiDA extras track completed deliveries to avoid duplicates.

| Option | Behavior |
| --- | --- |
| `--dry-run` | Scan and log without HTTP requests or AiiDA extra updates. |
| `--no-commit` | Send results without updating AiiDA extras. |
| `--resend-all` | Scan eligible parents once, including processed parents, then exit. Respect successful delivery markers and retry deliveries without them. |
| `--logging-level LEVEL`, `-l LEVEL` | Set `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. Default: `ERROR`. |

`--resend-all` still applies the configured hierarchy and monitor filters. Combine it with `--dry-run` for a single inspection scan. If you supply both `--dry-run` and `--no-commit`, the daemon uses dry-run mode.

### Submit one parent

```bash
aiida-mpds-submit 12345 --dry-run
aiida-mpds-submit 12345 --logging-level INFO
```

Replace `12345` with a parent WorkChain PK. This command uses the configured hierarchy and archive settings, bypasses `monitor_filters`, and leaves daemon delivery markers unchanged. It does not run the daemon's automatic CRYSTAL recovery or send Telegram reports.

### Test webhooks locally

Start the stub in another terminal:

```bash
aiida-mpds-stub
```

For this test, set `webhook_url: "http://localhost:8080"` and `send_archive: false` in the configuration. The stub prints incoming POST data and returns HTTP 200; it does not validate archives.

## Configuration

The daemon and submit command use `~/.aiida/aiida_mpds_monitor/conf.yaml`. If the file does not exist, they create it with the defaults from [config.py](aiida_mpds_monitor/config.py). Set the endpoints and a writable log path before sending results; the default log path is `/data/aiida_mpds_monitor.log`.

The daemon reloads YAML before each scan. Invalid updates produce a warning, and the daemon keeps the previous settings. Environment changes require a restart.

### Delivery and archives

| YAML key | Default | Purpose |
| --- | --- | --- |
| `webhook_url` | `http://localhost:8080` | Status webhook endpoint. |
| `auth_key` | Empty | Webhook authentication fallback. Prefer `MPDS_MONITOR_KEY`. |
| `poll_interval` | `30` | Seconds between scans. |
| `workchain_hierarchy` | CRYSTAL hierarchy shown above | Parent → child → calculation process labels. |
| `archive_upload_url` | Empty | Archive endpoint; see resolution order below. |
| `archive_key` | Empty | Archive authentication fallback. |
| `archive_bid` | Unset | Optional `bid` upload form field. |
| `archive_schema_id` | Unset | Optional `schema_id` upload form field. |
| `send_archive` | `true` | Enable archive generation and upload. |
| `send_archives_all_stages_ready` | `false` | Require all selected subcalculations to succeed before initial archive generation. |
| `archive_keep` | `false` | Keep local archives after successful uploads. Failed uploads retain the file. |
| `archive_keep_errors` | `false` | Preserve a separate copy of each server-rejected archive, including rejected recovery retries, for inspection. |
| `archive_errors_dir` | Empty | Directory for preserved rejected archives when `archive_keep_errors` is enabled. Empty saves beside the working archive. |

Authentication and endpoint resolution:

| Setting | Priority, highest first |
| --- | --- |
| Webhook key | `MPDS_MONITOR_KEY` environment variable → `auth_key` → legacy `security_key` |
| Archive key | `MPDS_ARCHIVE_KEY` environment variable → `archive_key` → webhook key |
| Archive URL | `archive_upload_url` → `ARCHIVE_UPLOAD_URL` environment variable → built-in MPDS upload endpoint |

Set `archive_upload_url` to choose the destination. Leaving both URL settings unset selects the built-in remote endpoint defined in [config.py](aiida_mpds_monitor/config.py).

For a separate archive token:

```bash
export MPDS_ARCHIVE_KEY="<archive-key>"
```

Status webhooks use form fields `payload`, `status`, and `key`. Status values include `waiting`, `finished`, and `excepted`, with an exit-code suffix where available. Archive uploads use a multipart `file` field, optional `bid` and `schema_id` fields, and an `Authorization: Bearer` header.

### Workflow filters

Filters apply to daemon MPDS deliveries, including `--resend-all`. Manual submission and Telegram reports ignore them.

```yaml
monitor_filters:
  created_after: "2026-08-01"
  created_before: "2026-08-31"
  max_age_hours: null
  element_counts: [2, 3]
  element_count_greater_than: null
  compounds: []
  elements: [Ba, Mn]
  elements_match: any
```

| Key under `monitor_filters` | Meaning |
| --- | --- |
| `created_after`, `created_before` | Inclusive bounds on parent creation time. Accept dates or ISO 8601 timestamps; date bounds include the complete day. |
| `max_age_hours` | Rolling age window. `168` selects the past seven days. Combine with `created_after` to use the stricter bound. |
| `element_counts` | Allowed numbers of distinct elements; `[2, 3]` selects binary and ternary compounds. |
| `element_count_greater_than` | Exclusive lower bound on the number of distinct elements. |
| `compounds` | Exact, case-sensitive formulas at the start of workflow labels. |
| `elements` | Element symbols to match in the leading formula. |
| `elements_match` | Match `any` configured element, or require `all`. Default: `any`. |

Enabled filters combine with AND. Empty lists and `null` disable their respective filters. Labels such as `BaPd3P/109: Geometry optimization` identify three distinct elements. Unrecognizable formulas fail enabled compound filters.

### Logging

| Key | Default | Purpose |
| --- | --- | --- |
| `log_file` | `/data/aiida_mpds_monitor.log` | Log file path. Relative paths use the working directory. |
| `log_max_bytes` | `10485760` | Rotation threshold in bytes. |
| `log_backup_count` | `3` | Number of rotated files to retain. |
| `log_level` | `WARNING` | YAML logging level. The daemon's CLI setting overrides it, including the CLI default of `ERROR`. |

Use `--logging-level INFO` to see routine delivery and cleanup messages.

## CRYSTAL archive recovery

### Automatic recovery

The daemon uploads the initial archive before running local CRYSTAL validation. Recovery runs only after HTTP 422 with this server explanation:

```text
Archive validation failed: Found not a finished output
```

The validator checks `STRUCT`, `PHON`/`PHONON`, and `ELAST`/`ELASTIC` folders with `pycrystal`. Checks cover output completion, fatal runtime errors, parsed structures and energies, MPDS input settings, optional `fort.34` geometry, phonon data, and elastic tensors. Valid stages must match the elements in `STRUCT` and differ from its energy by at most 0.01 eV.

Recovery follows these steps:

1. Record invalid folders as `broken` or `corrupted` in a sibling `*.validation.json` report.
2. Confirm broken outputs against the retrieved AiiDA `OUTPUT` using SHA-256.
3. Flag confirmed failures on `CrystalParallelCalculation` nodes and their containing `BaseCrystalWorkChain` nodes through `mpds_archive_validation` extras.
4. Remove invalid folders from a temporary archive copy, rebuild it, and retry the upload once.

Calculation flags include `exclude_from_archive: true`, a reason, and original process attributes. Future archive generation skips those calculations, including with strict stage readiness enabled. Healthy calculations remain eligible. AiiDA process states, sealing, and retrieved data stay intact; the next status scan reports flagged broken bases as `excepted`.

Input-policy failures and cross-stage inconsistencies do not prove that an AiiDA process failed. Unexpected parser errors, local I/O failures, or mismatched source outputs abort recovery without flagging the process.

A repaired archive requires a valid `STRUCT`. Recovery also requires mappings from invalid folders to CRYSTAL calculation nodes. A second upload failure retains the cleaned archive and a checksum in `mpds_archive_recovery`; later scans retry that file without rebuilding or revalidating it. Missing or modified pending files prevent upload.

`--no-commit` permits repair and upload without writing flags or delivery extras. `--dry-run` makes no upload request and cannot trigger recovery. Automatic recovery applies to the daemon; use the repair command below for standalone archives.

### Check or repair an archive

```bash
# Inspect without writing files or uploading.
aiida-mpds-repair-crystal material.7z --dry-run

# Create material.clean.7z and material.clean.validation.json.
aiida-mpds-repair-crystal material.7z

# Repair a directory and upload through the configured endpoint.
aiida-mpds-repair-crystal /path/to/material \
  --output /path/to/material.fixed.7z \
  --upload \
  --config ~/.aiida/aiida_mpds_monitor/conf.yaml
```

A directory must contain one material's stage folders. An archive may include a wrapping material directory. The command preserves the source, includes validated stage folders in the result, and retains repaired archives and reports after upload. Choose a new output name if output files already exist.

Standalone repair does not update AiiDA extras or daemon delivery markers. It sends an archive only with `--upload`; the server determines whether that upload replaces an existing record.

| Exit code | Meaning |
| --- | --- |
| `0` | Successful check, build, or upload. |
| `1` | Operational error. |
| `2` | No uploadable structure remains. |
| `3` | Upload failed. |

### Archive storage

Generated parent archives use `archives/<parent UUID>/<material>.7z` relative to the output root, which defaults to the working directory. `archive_keep: false` removes the local archive after a successful daemon or submit upload.

Set `archive_keep_errors: true` to retain server-rejected archives as `<material>.rejected-<SHA256>.7z` beside the working archive. The daemon saves the original before automatic recovery replaces it, and saves the repaired archive too if the server rejects the retry. The submit CLI also preserves server-rejected archives. Identical rejected contents share one saved copy; later scans and successful uploads leave these copies intact. Network failures continue to retain the working archive for retry.

Use `archive_errors_dir` to choose where these copies are saved:

```yaml
archive_keep_errors: true
archive_errors_dir: "~/aiida-unsent-archives"
```

The directory is created automatically. Relative paths are resolved from the monitor's working directory, and `~` expands to the user's home directory. An empty value keeps the default location beside the working archive. Working archives used for upload retries remain in their original location so pending recovery records stay valid.

Temporary calculation folders, intermediate archives, and generated summary files are removed after archive generation, including on failure. Successful uploads also remove the sibling validation report and empty archive directories unless `archive_keep` or `archive_keep_errors` retains the diagnostics. Failed uploads retain their working archive and report. Persistent lock files remain for coordination between local monitors. Standalone repair commands continue to retain their outputs and reports.

Local daemons use a per-parent POSIX lock across generation, recovery, and upload. Locks coordinate processes using the same working directory, not monitors on different hosts. Recovery rolls back flags and restores the rejected archive on ordinary publication or database commit failures; filesystem and database changes do not form a distributed transaction.

The standalone extractor rejects traversal paths, duplicate member names, links, and special files. Its default limits are 10,000 members and 20 GiB of unpacked data.

## Telegram reports

### Setup

Create a bot with [@BotFather](https://t.me/BotFather), add it to your group, and allow it to send messages. Obtain the group chat ID through Telegram's `getUpdates` API; see the [bot setup guide](https://core.telegram.org/bots/tutorial).

Set credentials in the daemon's environment:

```bash
export TELEGRAM_BOT_TOKEN="<bot-token>"
export TELEGRAM_CHAT_ID="<group-chat-id>"
```

Add the report settings to `conf.yaml`:

```yaml
running_alert_hours: 24
notification_time: "09:00"
notification_timezone: "UTC"
notification_user_names:
  "alice@example.org": "@alice"
  "bob@example.org": "Bob Smith"
```

Both credentials are required. Each setting resolves from its environment variable, then the lowercase YAML key (`telegram_bot_token` or `telegram_chat_id`), then the uppercase YAML alias. Restrict access to configuration files that contain credentials:

```bash
chmod 600 ~/.aiida/aiida_mpds_monitor/conf.yaml
```

`notification_user_names` maps AiiDA owner email addresses to display names. Find owners with `verdi user list`. For a profile with one owner, `notification_user_name: "@alice"` sets a fallback name for unmapped owners. Otherwise, reports use the AiiDA owner's name or email.

### Schedule and contents

The daemon sends a report after its first successful scan and once per day at `notification_time` in `notification_timezone`. A startup at or after that day's scheduled time counts as the daily check. Restarting the daemon triggers another startup report. YAML edits apply on the next scan without an extra startup report.

Reports cover running processes listed at any level of `workchain_hierarchy`. Details include the owner, label, PK, hostname when available, and elapsed runtime for calculations exceeding `running_alert_hours`. Statistics count selected running processes, including those within the limit. `running_alert_hours: null` disables overdue details while retaining statistics.

The duration threshold must be positive; fractional hours are supported. Runtime uses the scheduler's start time or the monitor's first observation as a lower bound. Normal runs persist fallback timers in AiiDA extras; `--no-commit` keeps them in memory.

The allocated-server count requires configured `yascheduler` and `pg8000` installations. Reports show `unavailable` if the monitor cannot read that inventory. Instances sharing a scheduler database report the same inventory.

### Send a report on demand

```bash
aiida-mpds-notify
aiida-mpds-notify --overdue-only
aiida-mpds-notify --config /path/to/conf.yaml
```

The default command includes details for all selected running calculations. `--overdue-only` applies the configured runtime threshold. Both send statistics without changing the daemon's schedule or sending MPDS results.

### Delivery behavior

Reports use plain-text chunks of up to 2,000 characters. Send attempts within one monitor are at least 3.1 seconds apart. Explicit rate-limit responses allow up to two retries per rejected chunk if `retry_after` is an integer from 0 through 60 seconds. Other failures log an error without a retry.

A scheduled report consumes its slot even if delivery fails. The monitor attempts later chunks and statistics after an earlier failure. Requests and retry waits run in the daemon loop and can delay MPDS processing. Separate monitor instances do not coordinate Telegram send timing.

Archive upload failures queue one error notice per failing endpoint for the next report. A successful upload clears that notice. Telegram errors do not stop MPDS monitoring. The bot sends reports; it does not handle chat commands or completion alerts.

Run one monitor per AiiDA profile to avoid duplicate reports. Separate profiles may share a bot and chat.

## Contributing

Install the development dependencies, then run the tests:

```bash
python -m pip install -e '.[dev]'
python -m pytest
```

The suite uses mocks and temporary AiiDA storage. Pytest writes terminal coverage and an HTML report in `htmlcov/`. The repository has no configured lint or type-check command.

Keep changes focused, add tests for behavior changes, and update configuration examples when settings change. Review status handling, webhook fields, and archive layout against the MPDS backend contract. See [AGENTS.md](AGENTS.md) for repository conventions.

## License

[MIT](LICENSE). Copyright © 2026 Materials Platform for Data Science OÜ.
