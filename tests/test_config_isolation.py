"""Tests for multi-bot configuration isolation."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import click

from nanobot.config.loader import get_config_path, get_data_dir, load_config

# ---------------------------------------------------------------------------
# get_config_path — env var support
# ---------------------------------------------------------------------------


def test_get_config_path_default():
    """Without NANOBOT_CONFIG, returns ~/.nanobot/config.json."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NANOBOT_CONFIG", None)
        result = get_config_path()
    assert result == Path.home() / ".nanobot" / "config.json"


def test_get_config_path_env_var(tmp_path: Path):
    """NANOBOT_CONFIG overrides the default path."""
    custom = tmp_path / "custom" / "config.json"
    with patch.dict(os.environ, {"NANOBOT_CONFIG": str(custom)}):
        result = get_config_path()
    assert result == custom.resolve()


def test_get_config_path_env_var_empty():
    """Empty NANOBOT_CONFIG falls back to default."""
    with patch.dict(os.environ, {"NANOBOT_CONFIG": ""}):
        result = get_config_path()
    assert result == Path.home() / ".nanobot" / "config.json"


def test_get_config_path_env_var_whitespace():
    """Whitespace-only NANOBOT_CONFIG falls back to default."""
    with patch.dict(os.environ, {"NANOBOT_CONFIG": "   "}):
        result = get_config_path()
    assert result == Path.home() / ".nanobot" / "config.json"


# ---------------------------------------------------------------------------
# get_data_dir — derived from config path
# ---------------------------------------------------------------------------


def test_get_data_dir_from_config_path(tmp_path: Path):
    """Data dir is the parent directory of the config file."""
    config_file = tmp_path / "mybot" / "config.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    result = get_data_dir(config_path=config_file)
    assert result == config_file.resolve().parent


def test_get_data_dir_none_falls_back():
    """Without config_path, falls back to default data dir."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NANOBOT_CONFIG", None)
        result = get_data_dir(config_path=None)
    assert result == Path.home() / ".nanobot"


def test_data_isolation_two_configs(tmp_path: Path):
    """Two different config paths produce two different data dirs."""
    bot1_cfg = tmp_path / "bot1" / "config.json"
    bot2_cfg = tmp_path / "bot2" / "config.json"
    bot1_cfg.parent.mkdir(parents=True)
    bot2_cfg.parent.mkdir(parents=True)

    dir1 = get_data_dir(config_path=bot1_cfg)
    dir2 = get_data_dir(config_path=bot2_cfg)

    assert dir1 != dir2
    assert dir1 == bot1_cfg.resolve().parent
    assert dir2 == bot2_cfg.resolve().parent


# ---------------------------------------------------------------------------
# load_config — explicit config path
# ---------------------------------------------------------------------------


def test_load_config_explicit_path(tmp_path: Path):
    """load_config reads from the specified path."""
    config_file = tmp_path / "config.json"
    data = {"gateway": {"port": 19999}}
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(config_path=config_file)
    assert cfg.gateway.port == 19999


def test_load_config_missing_file_returns_default(tmp_path: Path):
    """Missing config file returns default Config."""
    config_file = tmp_path / "nonexistent.json"
    cfg = load_config(config_path=config_file)
    assert cfg.gateway.port == 18790


# ---------------------------------------------------------------------------
# _resolve_port — port precedence
# ---------------------------------------------------------------------------


def test_resolve_port_cli_explicit():
    """When --port is passed on CLI, CLI value wins."""
    from nanobot.cli.commands import _resolve_port

    ctx = MagicMock(spec=["get_parameter_source"])
    ctx.get_parameter_source.return_value = click.core.ParameterSource.COMMANDLINE

    result = _resolve_port(ctx, cli_port=19000, config_port=18790)
    assert result == 19000


def test_resolve_port_config_wins_on_default():
    """When --port is not passed, config value wins."""
    from nanobot.cli.commands import _resolve_port

    ctx = MagicMock(spec=["get_parameter_source"])
    ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT

    result = _resolve_port(ctx, cli_port=18790, config_port=19001)
    assert result == 19001


def test_resolve_port_cli_explicit_same_as_default():
    """Even if CLI passes the same value as default, CLI source wins."""
    from nanobot.cli.commands import _resolve_port

    ctx = MagicMock(spec=["get_parameter_source"])
    ctx.get_parameter_source.return_value = click.core.ParameterSource.COMMANDLINE

    result = _resolve_port(ctx, cli_port=18790, config_port=19001)
    assert result == 18790
