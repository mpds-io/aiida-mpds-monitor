
import os
from pathlib import Path
from typing import Optional

import yaml
from aiida.common.extendeddicts import AttributeDict


DEFAULT_CONFIG_PATH = Path.home() / ".aiida" / "aiida_mpds_monitor" / "conf.yaml"

DEFAULT_CONFIG = {
    "webhook_url": "http://localhost:8080",
    "auth_key": "",
    "poll_interval": 30,
    "running_alert_hours": None,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "notification_time": "09:00",
    "notification_timezone": "UTC",
    "notification_user_name": "",
    "notification_user_names": {},
    "workchain_hierarchy": {
        "MPDSStructureWorkChain": {
            "BaseCrystalWorkChain": ["CrystalParallelCalculation"]
        }
    },
    "log_file": "/data/aiida_mpds_monitor.log",
    "log_level": "WARNING",  # INFO, DEBUG, WARNING, ERROR
    "log_max_bytes": 10 * 1024 * 1024,  # 10 MB
    "log_backup_count": 3,
    "archive_upload_url": "",
    "archive_keep": False,
    "archive_key": "",
    "send_archive": True,
    "send_archives_all_stages_ready": False,
    "monitor_filters": {
        "created_after": None,
        "created_before": None,
        "max_age_hours": None,
        "element_counts": [],
        "element_count_greater_than": None,
        "compounds": [],
        "elements": [],
        "elements_match": "any",
    },
}


def ensure_config_dir():
    config_dir = DEFAULT_CONFIG_PATH.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o755)
    return DEFAULT_CONFIG_PATH


def read_config(config_path: Optional[Path] = None) -> AttributeDict:
    """Read an existing configuration without creating or modifying files."""
    with open(config_path or DEFAULT_CONFIG_PATH) as f:
        user_config = yaml.safe_load(f)
    if user_config is None:
        user_config = {}
    if not isinstance(user_config, dict):
        raise ValueError("Configuration must be a YAML mapping")
    return AttributeDict({**DEFAULT_CONFIG, **user_config})


def load_config():
    config_path = ensure_config_dir()

    if not config_path.exists():
        print(f"Creating default config at {config_path}")
        with open(config_path, "w") as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False)
        config_path.chmod(0o644)

    return read_config(config_path)


def get_auth_key(conf):
    return (
        os.environ.get("MPDS_MONITOR_KEY")
        or conf.get("auth_key")
        or conf.get("security_key")
    )


def get_archive_key(conf):
    return os.environ.get("MPDS_ARCHIVE_KEY") or conf.get("archive_key") or get_auth_key(conf)


def resolve_archive_upload_url(conf, logger):
    return conf.get("archive_upload_url") or os.environ.get("ARCHIVE_UPLOAD_URL",
        "https://esdd.tilde.pro/api/v1/tasks/upload/absolidix")
