"""V2 §7 guild config — env global default < guild_config override.

Public API:
  get_guild_config(db, guild_id, key)  — resolve one key with precedence
  resolve_config(db, guild_id)         — all §7 keys with current values
  set_config(db, guild_id, key, value) — validate + write guild_config
  channel_allowed(db, guild_id, channel_id) — allowed_channels gate
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from bot.db import CourtDB

# §7 keys → (default, env var name, python type tag)
# type tags: int | float | bool | channels
CONFIG_KEYS: dict[str, tuple[Any, str | None, str]] = {
    "counters_per_side": (2, "FIGHT_COUNTERS_PER_SIDE", "int"),
    "challenge_timeout_hours": (6.0, "FIGHT_CHALLENGE_TIMEOUT_HOURS", "float"),
    "rest_timeout_hours": (24.0, "FIGHT_REST_TIMEOUT_HOURS", "float"),
    "balance_warn_below": (4.0, "FIGHT_BALANCE_WARN_BELOW", "float"),
    "balance_free_counter": (True, "FIGHT_BALANCE_FREE_COUNTER", "bool"),
    "cooldown_seconds": (60, "FIGHT_COOLDOWN_SECONDS", "int"),
    "daily_cap": (50, "FIGHT_GUILD_DAILY_CAP", "int"),
    "monthly_usd_cap": (20.0, "FIGHT_MONTHLY_USD_CAP", "float"),
    "allowed_channels": ([], "FIGHT_ALLOWED_CHANNELS", "channels"),
    "thread_archive_delay_hours": (24.0, "FIGHT_THREAD_ARCHIVE_DELAY_HOURS", "float"),
    "transcript_max_tokens": (20_000, "FIGHT_TRANSCRIPT_MAX_TOKENS", "int"),
    "sweep_interval_minutes": (2, "FIGHT_SWEEP_INTERVAL_MINUTES", "int"),
}

KNOWN_KEYS = frozenset(CONFIG_KEYS.keys())

_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off", ""})


class ConfigError(ValueError):
    """Invalid config key or value."""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in _BOOL_TRUE:
        return True
    if s in _BOOL_FALSE:
        return False
    raise ConfigError(f"invalid boolean: {value!r}")


def _parse_channels(raw: Any) -> list[int]:
    """Parse allowed_channels: empty / [] = all; else list of channel snowflakes."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        out: list[int] = []
        for item in raw:
            out.append(int(item))
        return out
    s = str(raw).strip()
    if not s or s in {"[]", "null", "none", "*"}:
        return []
    if s.startswith("["):
        try:
            data = json.loads(s)
        except json.JSONDecodeError as e:
            raise ConfigError(f"invalid allowed_channels JSON: {e}") from e
        if not isinstance(data, list):
            raise ConfigError("allowed_channels JSON must be a list")
        return [int(x) for x in data]
    # comma / whitespace separated ids
    parts = re.split(r"[\s,]+", s)
    out = []
    for p in parts:
        if not p:
            continue
        try:
            out.append(int(p))
        except ValueError as e:
            raise ConfigError(f"invalid channel id: {p!r}") from e
    return out


def _coerce(raw: Any, type_tag: str, default: Any) -> Any:
    if raw is None:
        return default
    if type_tag == "int":
        return int(raw)
    if type_tag == "float":
        return float(raw)
    if type_tag == "bool":
        return _as_bool(raw)
    if type_tag == "channels":
        return _parse_channels(raw)
    return raw


def _env_raw(env_name: str | None) -> str | None:
    if not env_name:
        return None
    val = os.getenv(env_name)
    if val is None:
        return None
    if not str(val).strip() and env_name != "FIGHT_ALLOWED_CHANNELS":
        # empty string for numeric keys → fall through to default
        return None
    return val


def _format_stored(value: Any, type_tag: str) -> str:
    """Canonical string stored in guild_config."""
    if type_tag == "bool":
        return "true" if _as_bool(value) else "false"
    if type_tag == "channels":
        channels = _parse_channels(value)
        return json.dumps(channels)
    if type_tag == "int":
        return str(int(value))
    if type_tag == "float":
        # keep ints clean when whole numbers
        f = float(value)
        if f.is_integer():
            return str(int(f))
        return str(f)
    return str(value)


def format_config_value(value: Any, key: str) -> str:
    """Human-readable value for /config list."""
    meta = CONFIG_KEYS.get(key)
    type_tag = meta[2] if meta else "str"
    if type_tag == "bool":
        return "true" if bool(value) else "false"
    if type_tag == "channels":
        channels = value if isinstance(value, list) else _parse_channels(value)
        return "[]" if not channels else json.dumps(channels)
    return str(value)


def _coerce_guild_id(guild_id: Any) -> int | None:
    """Accept real int/str ids only (ignore MagicMock and other hosts)."""
    if guild_id is None or isinstance(guild_id, bool):
        return None
    if isinstance(guild_id, int):
        return int(guild_id)
    if isinstance(guild_id, str) and str(guild_id).strip():
        try:
            return int(str(guild_id).strip())
        except ValueError:
            return None
    return None


def get_guild_config(
    db: CourtDB | None,
    guild_id: int | None,
    key: str,
) -> Any:
    """Resolve one §7 key: guild_config override → env → hardcoded default."""
    if key not in CONFIG_KEYS:
        raise ConfigError(f"unknown config key: {key}")
    default, env_name, type_tag = CONFIG_KEYS[key]

    gid = _coerce_guild_id(guild_id)
    if db is not None and gid is not None:
        raw = db.get_guild_config(gid, key)
        # Only trust concrete scalars from SQLite (skip MagicMock etc.).
        if raw is not None and not isinstance(raw, (str, int, float, bool)):
            raw = None
        if raw is not None and str(raw).strip() != "":
            try:
                return _coerce(raw, type_tag, default)
            except (TypeError, ValueError, ConfigError):
                pass
        elif raw is not None and type_tag == "channels" and str(raw).strip() == "":
            return []

    env_raw = _env_raw(env_name)
    if env_raw is not None:
        try:
            return _coerce(env_raw, type_tag, default)
        except (TypeError, ValueError, ConfigError):
            pass

    return default


def resolve_config(
    db: CourtDB | None,
    guild_id: int | None,
) -> dict[str, Any]:
    """All §7 keys with current effective values (precedence applied)."""
    return {k: get_guild_config(db, guild_id, k) for k in CONFIG_KEYS}


def set_config(
    db: CourtDB,
    guild_id: int,
    key: str,
    value: str,
) -> Any:
    """Validate key+value, write guild_config, return coerced value."""
    if key not in CONFIG_KEYS:
        raise ConfigError(
            f"unknown config key: {key}. Valid keys: {', '.join(sorted(KNOWN_KEYS))}"
        )
    _default, _env, type_tag = CONFIG_KEYS[key]
    try:
        coerced = _coerce(value, type_tag, _default)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"invalid value for {key}: {value!r} ({e})") from e
    stored = _format_stored(coerced, type_tag)
    db.set_guild_config(int(guild_id), key, stored)
    return coerced


def channel_allowed(
    db: CourtDB | None,
    guild_id: int | None,
    channel_id: int | None,
) -> bool:
    """True when allowed_channels is empty (all) or channel_id is listed."""
    allowed = get_guild_config(db, guild_id, "allowed_channels")
    if not allowed:
        return True
    if channel_id is None:
        return False
    return int(channel_id) in {int(c) for c in allowed}


def list_config_lines(db: CourtDB | None, guild_id: int | None) -> list[str]:
    """Lines for /config list display."""
    resolved = resolve_config(db, guild_id)
    lines = []
    for key in CONFIG_KEYS:
        lines.append(f"`{key}` = {format_config_value(resolved[key], key)}")
    return lines
