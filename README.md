# aiida-mpds-monitor

A lightweight daemon and CLI tool to monitor AiiDA workflows and automatically send webhooks when configured child workchains complete. Designed for integration with external job-tracking systems (e.g., MPDS backend). Fully configurable hierarchy of parent -> child -> grandchild workchain monitoring with flexible status checks.

## Installation
Install from source (recommended):

```bash
git clone https://github.com/mpds-io/aiida-mpds-monitor.git
cd aiida-mpds-monitor
pip install .
```

## Workflow Label Requirement

> [!IMPORTANT]
> The **label** field of your AiiDA workflow is critical for webhook delivery and server-side processing. aiida-mpds-monitor expects that the tag is an identifier that allows the server to understand which object this workflow belongs to and which specific task was solved. Therefore, if the workflow does not have a tag, the monitor simply will not send a request to the server.
> Мake sure that each workflow you send has a label, for example: 'HgI2/137: Geometry optimization'

## Configuration
On first run, the tool creates a default config file:

System-wide: `/etc/aiida_mpds_monitor/conf.yaml`
Fallback (user): `~/.config/aiida_mpds_monitor/conf.yaml` (if no write access to /etc)

```yaml
# Webhook endpoint
webhook_url: "http://localhost:8080"

# How often to scan AiiDA database (seconds)
poll_interval: 60

# Unified workchain hierarchy: parent → child → grandchild
# Specifies which workchains to monitor and which children/grandchildren to check
workchain_hierarchy:
  MPDSStructureWorkChain:
    BaseCrystalWorkChain:
      - CrystalParallelCalculation

# Logging
log_file: "/path/to/logs/aiida_mpds_monitor.log"
log_level: "WARNING"          # DEBUG, INFO, WARNING, ERROR
log_max_bytes: 10485760       # 10 MB per log file
log_backup_count: 5           # Keep 5 rotated logs
```

## Usage
1. Configure the workchain hierarchy:

```yaml
# conf.yaml
webhook_url: "http://example.com/webhook"
workchain_hierarchy:
  ParentType:
    ChildType:
      - GrandchildType1
```

2. Set the authentication key and run the daemon:
```bash
export MPDS_MONITOR_KEY="your-api-key"
aiida-mpds-monitor
```

The daemon will:

* Scan for new parent workflows (matching configured types) every `poll_interval` seconds.
* For each parent, search for configured child workchains.
* For each child, check if any configured grandchild calculations failed.
* Send a webhook with the status when processing is complete.
* Automatically mark processed workflows to avoid duplicates.

Options:

  `--dry-run`: Dry-run mode — scans nodes and logs actions but does not send webhooks or mark nodes.

  `--no-commit`: Run and send webhooks, but do not mark processed workflows. For recovery or one-off runs.

  `--resend-all`: Force sending of every eligible webhook, ignoring any existing `webhook_finished` or
    `webhook_parent_processed` extras. Useful to recover from missed notifications.

  `--logging-level` / `-l`: Set the runtime logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL). If omitted, the tools default to `ERROR`.

⚠️ `--dry-run` and `--no-commit` are mutually exclusive - `--dry-run` takes precedence.

Examples:

```bash
# Default logging level (ERROR)
aiida-mpds-submit 12345

# Explicit logging level for more verbosity
aiida-mpds-submit 12345 --logging-level INFO

# Run daemon with debug logging
aiida-mpds-monitor --logging-level DEBUG
```

3. Manually submit results for a parent workflow:
Useful for backfilling or debugging:

```bash
# Send webhooks for all configured children of parent PK=12345
export MPDS_MONITOR_KEY="your-api-key"
aiida-mpds-submit 12345

# Dry-run: see what would be sent (no HTTP request)
aiida-mpds-submit 12345 --dry-run
```

## Testing with Stub Server
A built-in stub webhook server is included for local testing:

```bash
aiida-mpds-stub
```

This starts a server at http://localhost:8080 that accepts webhook payloads and prints them to the console.

## Architecture

The system uses a **hierarchical configuration** approach:

1. **Parent workchains**: Top-level workflows to monitor (configurable)
2. **Child workchains**: Expected calculations under each parent (configurable)
3. **Grandchild checks**: Validation of specific child process types (configurable)

This allows monitoring any workflow hierarchy without code changes - simply update the YAML configuration.

Copyright © 2026 Materials Platform for Data Science OÜ
