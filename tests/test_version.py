"""One version everywhere. plugin.yaml is what Hermes reads and pyproject.toml
is what pip sees; release-please bumps both."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_version_matches_the_release_manifest():
    released = json.loads((ROOT / ".release-please-manifest.json").read_text())["."]
    plugin = re.search(r"^version:\s*(\S+)\s*$", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
    project = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert (plugin, project) == (released, released)
