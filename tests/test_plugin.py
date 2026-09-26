"""The plugin only starts the SDK: once, with a key, and never at Hermes' expense."""
import logging
import sys
import ast
import re
import types
from pathlib import Path

import pytest
import yaml

import __init__ as plugin

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sdk(monkeypatch):
    """A stand-in `mnfst` module that records each manifest() call."""
    calls = []
    module = types.ModuleType("mnfst")
    module.manifest = lambda **kwargs: calls.append(kwargs)
    monkeypatch.setitem(sys.modules, "mnfst", module)
    return calls


def test_register_starts_the_sdk_once(monkeypatch, sdk):
    monkeypatch.setenv("MNFST_KEY", "mnfst_test")
    plugin.register(object())
    assert sdk == [{}]   # the SDK reads MNFST_* from the environment itself


def test_register_does_nothing_without_a_key(monkeypatch, sdk, caplog):
    monkeypatch.setenv("MNFST_KEY", "  ")
    with caplog.at_level(logging.WARNING):
        plugin.register(object())
    assert sdk == []
    assert "MNFST_KEY is not set" in caplog.text


def test_a_missing_sdk_is_reported_not_raised(monkeypatch, caplog):
    monkeypatch.setenv("MNFST_KEY", "mnfst_test")
    monkeypatch.setitem(sys.modules, "mnfst", None)   # makes the import fail
    with caplog.at_level(logging.WARNING):
        plugin.register(object())
    assert "mnfst package is not installed" in caplog.text


def test_an_sdk_that_fails_to_start_never_reaches_hermes(monkeypatch, caplog):
    monkeypatch.setenv("MNFST_KEY", "mnfst_test")
    module = types.ModuleType("mnfst")

    def broken(**kwargs):
        raise RuntimeError("boom")

    module.manifest = broken
    monkeypatch.setitem(sys.modules, "mnfst", module)
    with caplog.at_level(logging.WARNING):
        plugin.register(object())
    assert "mnfst failed to start: boom" in caplog.text


def test_mnfst_is_declared_once_where_dependabot_updates_it():
    """Hermes installs from pyproject.toml; a copy in plugin.yaml would drift from Dependabot's bumps."""
    line = re.search(r"^dependencies = (\[.*\])$", (ROOT / "pyproject.toml").read_text(), re.M)
    assert any(spec.startswith("mnfst>=") for spec in ast.literal_eval(line.group(1)))
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert "python_dependencies" not in manifest and "pip_dependencies" not in manifest
