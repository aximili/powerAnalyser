"""Persistent GUI settings (NEM12 path, LLM config, task prompt).

Settings are stored as JSON in the **project root** (``.gui_settings.json``,
next to ``pyproject.toml``) so they live alongside the code and survive
restarts.  For backward compatibility, a legacy copy in the per-user platform
config directory is migrated on first run.

Both :func:`load_settings` and :func:`save_settings` are fault-tolerant: a
missing/corrupt file or an unwritable directory simply yields the defaults /
a silent no-op rather than raising.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_SETTINGS_FILE = ".gui_settings.json"
_LEGACY_SETTINGS_FILE = "gui_settings.json"

#: Known keys and their defaults.  Adding a key here is enough to make it
#: load/save automatically — see :func:`load_settings` / :func:`save_settings`.
DEFAULTS: dict[str, Any] = {
    # Analyse tab
    "nem12_path": "",
    # Agent / Manual tabs (shared LLM config)
    "llm_provider": "ollama",
    "llm_model": "",
    "api_key_or_url": "",
    "target_url": "",
    "task_prompt": "",
}


def _project_root() -> Path:
    """Return the project root (the directory containing ``pyproject.toml``).

    Anchored to this source file so the location is stable regardless of the
    current working directory.  Falls back to the CWD if no ``pyproject.toml``
    is found (e.g. a non-editable install in site-packages).
    """
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _platform_config_dir() -> Path:
    """Return the legacy per-user platform config directory (for migration)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
    elif sys.platform == "darwin":
        base = os.path.join(str(Path.home()), "Library", "Application Support")
    else:  # Linux / other Unix
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            str(Path.home()), ".config"
        )
    return Path(base) / "PowerAnalyser"


def settings_path() -> Path:
    """Absolute path to the JSON settings file (in the project root)."""
    return _project_root() / _SETTINGS_FILE


def _legacy_settings_path() -> Path:
    return _platform_config_dir() / _LEGACY_SETTINGS_FILE


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_settings() -> dict[str, Any]:
    """Load settings, merged over :data:`DEFAULTS`.

    Never raises — returns defaults on any read/parse failure.  If the project
    settings file does not yet exist, a legacy platform-location copy is read
    once so existing users keep their settings after the move.
    """
    merged = dict(DEFAULTS)

    data = _read_json(settings_path())
    if data is None and not settings_path().exists():
        data = _read_json(_legacy_settings_path())  # one-time migration source

    if isinstance(data, dict):
        for key in DEFAULTS:
            if key in data:
                merged[key] = data[key]
    return merged


def save_settings(data: dict[str, Any]) -> None:
    """Persist a subset of *data* (only known keys) to the settings file.

    Never raises — a write failure is silently ignored so closing the app
    never produces an error dialog about settings.
    """
    to_save = {key: data.get(key, DEFAULTS[key]) for key in DEFAULTS}
    try:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(to_save, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        pass
