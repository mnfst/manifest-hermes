"""Keep the measurement sink out of the developer's real ~/.hermes while testing."""
import pytest


@pytest.fixture(autouse=True)
def _trace_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TRACE_DIR", str(tmp_path / "hermes-trace"))
