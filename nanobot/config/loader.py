"""Configuration loading utilities."""

import json
import os
from pathlib import Path

from nanobot.config.schema import Config


def get_config_path() -> Path:
    """Get the configuration file path.

    Resolution order:
    1. NANOBOT_CONFIG environment variable (if set and non-empty)
    2. Default: ~/.nanobot/config.json
    """
    env_path = os.environ.get("NANOBOT_CONFIG", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path.home() / ".nanobot" / "config.json"


def get_data_dir(config_path: Path | None = None) -> Path:
    """Get the nanobot data directory, derived from the config file location.

    The data directory is the parent directory of the config file.
    For config at ~/.nanobot/config.json, data dir is ~/.nanobot/.
    For config at /opt/bots/bot1/config.json, data dir is /opt/bots/bot1/.

    Args:
        config_path: Explicit config file path. If None, uses get_config_path().
    """
    from nanobot.utils.helpers import ensure_dir

    path = config_path or get_config_path()
    return ensure_dir(path.resolve().parent)


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            return Config.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data
