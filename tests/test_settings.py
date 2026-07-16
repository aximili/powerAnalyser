"""Tests for GUI settings persistence (project-root JSON file).

Runs headless — ``settings.py`` imports no GUI toolkit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from power_analyser.gui import settings as s


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect settings to a temp file so tests never touch the real one."""
    target = tmp_path / ".gui_settings.json"
    monkeypatch.setattr(s, "settings_path", lambda: target)
    return target


def test_save_then_load_round_trip(isolated_settings: Path) -> None:
    payload = {
        "nem12_path": "/data/sample.csv",
        "llm_provider": "glm",
        "llm_model": "glm-4v",
        "api_key_or_url": "secret-key",
        "target_url": "https://example.com/plans",
        "task_prompt": "find plans",
        # unknown keys must be ignored
        "rogue_key": "should not persist",
    }
    s.save_settings(payload)
    assert isolated_settings.exists()

    loaded = s.load_settings()
    assert loaded["nem12_path"] == "/data/sample.csv"
    assert loaded["llm_provider"] == "glm"
    assert loaded["api_key_or_url"] == "secret-key"
    assert "rogue_key" not in loaded


def test_save_only_persists_known_keys(isolated_settings: Path) -> None:
    s.save_settings({"nem12_path": "/x.csv", "extra": "noise"})
    on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert set(on_disk.keys()) == set(s.DEFAULTS.keys())


def test_load_returns_defaults_when_file_missing(isolated_settings: Path) -> None:
    assert not isolated_settings.exists()
    loaded = s.load_settings()
    assert loaded == s.DEFAULTS


def test_load_recovers_from_corrupt_file(isolated_settings: Path) -> None:
    isolated_settings.write_text("{ not valid json", encoding="utf-8")
    assert s.load_settings() == s.DEFAULTS


def test_settings_path_is_in_project_root() -> None:
    """The real settings file must live in the project root (next to pyproject.toml)."""
    path = s.settings_path()
    assert path.name == ".gui_settings.json"
    assert (path.parent / "pyproject.toml").exists()


def test_period_keys_in_defaults_and_round_trip(isolated_settings: Path) -> None:
    """The three analysis-period keys must exist in DEFAULTS and round-trip."""
    for key in ("period_mode", "period_from", "period_to"):
        assert key in s.DEFAULTS
    assert s.DEFAULTS["period_mode"] == "all"
    assert s.DEFAULTS["period_from"] == ""
    assert s.DEFAULTS["period_to"] == ""

    payload = {
        "period_mode": "custom",
        "period_from": "1/6",
        "period_to": "31/8",
    }
    s.save_settings(payload)
    loaded = s.load_settings()
    assert loaded["period_mode"] == "custom"
    assert loaded["period_from"] == "1/6"
    assert loaded["period_to"] == "31/8"

