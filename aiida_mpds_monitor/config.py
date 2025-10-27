
import os
import yaml
from pathlib import Path

from aiida.common.extendeddicts import AttributeDict


DEFAULT_CONFIG_PATH = Path("/etc/aiida_mpds_monitor/conf.yaml")

DEFAULT_CONFIG = {
    "webhook_url": "http://localhost:8080",
    "poll_interval": 30,
    "workchain_types": [
        "MPDSStructureWorkChain"
    ],
    "log_file": "/data/aiida_mpds_monitor.log",
    "log_level": "WARNING", # INFO, DEBUG, INFO, WARNING, ERROR
    "log_max_bytes": 10 * 1024 * 1024,  # 10 MB
    "log_backup_count": 3,
}


def ensure_config_dir():
    config_dir = DEFAULT_CONFIG_PATH.parent
    if not config_dir.exists():
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(config_dir, 0o755)
        except PermissionError:
            fallback = Path.home() / ".config/aiida_mpds_monitor/conf.yaml"
            return fallback
    return DEFAULT_CONFIG_PATH


def load_config():
    config_path = ensure_config_dir()

    if not config_path.exists():
        print(f"Creating default config at {config_path}")
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w") as f:
                yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False)
            config_path.chmod(0o644)
        except PermissionError:
            fallback = Path.home() / ".config/aiida_mpds_monitor/conf.yaml"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            print(f"Using fallback config: {fallback}")
            with open(fallback, "w") as f:
                yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False)
            config_path = fallback

    with open(config_path) as f:
        user_config = yaml.safe_load(f) or {}

    # Merging user config with defaults
    final_config = {**DEFAULT_CONFIG, **user_config}
    return AttributeDict(final_config)
