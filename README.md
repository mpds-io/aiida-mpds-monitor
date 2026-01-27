# aiida-mpds-monitor

A lightweight daemon and CLI tool to monitor AiiDA workflows (e.g., MPDSStructureWorkChain) and automatically send webhooks when child BaseCrystalWorkChain calculations start or finish. Designed for integration with external job-tracking systems (e.g., MPDS backend).

## Installation
Install from source (recommended):
```bash
git clone https://github.com/mpds-io/aiida-mpds-monitor.git
cd aiida-mpds-monitor
pip install .
```

## Configuration
On first run, the tool creates a default config file: 

System-wide: `/etc/aiida_mpds_monitor/conf.yaml`
Fallback (user): `~/.config/aiida_mpds_monitor/conf.yaml` (if no write access to /etc)

```yaml
# Webhook endpoint
webhook_url: "http://localhost:8080"

# Optional: API key for webhook authentication (Bearer token)
key: ""

# How often to scan AiiDA database (seconds)
poll_interval: 60

# WorkChain types to monitor (must match `process_label`)
workchain_types:
  - "MPDSStructureWorkChain"

# Logging
log_file: "/path/to/logs/aiida_mpds_monitor.log"
log_level: "WARNING"          # DEBUG, INFO, WARNING, ERROR
log_max_bytes: 10485760       # 10 MB per log file
log_backup_count: 5           # Keep 5 rotated logs
```

## Usage
1. Run the background monitor:
```bash
aiida-mpds-monitor
```

The daemon will: 

* Scan for new parent workflows every `poll_interval` seconds.
* For each BaseCrystalWorkChain with a label:
    * Send webhook with status when it finishes.
    * Automatically detect child calculation failures.
         
* Mark processed workflows to avoid duplicates.

Options: 

    `--dry-run`: Dry-run mode — scans nodes and logs actions but does not send webhooks or mark nodes.
    `--no-marks`: Run and send webhooks on server, but do not mark them as processed. For recovery or one-off runs.

⚠️ `--dry-run` and `--no-marks` are mutually exclusive - `--dry-run` takes precedence.


2. Manually submit results for a parent workflow:
Useful for backfilling or debugging:
```bash
# Send webhooks for all child calculations of parent PK=12345
aiida-mpds-submit PARENT_PK

# Dry-run: see what would be sent (no HTTP request)
aiida-mpds-submit PARENT_PK --dry-run
```

## Testing with Stub Server
A built-in stub webhook server is included for local testing:

```bash
aiida-mpds-stub
```

This starts a server at http://localhost:8080 that prints all received webhook payloads to the console.

Copyright © 2025 Anton Domnin