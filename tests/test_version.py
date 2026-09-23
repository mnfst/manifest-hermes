"""One version everywhere. plugin.yaml is what Hermes reads, VERSION is what
the plugin sends in its User-Agent, and pyproject.toml is what pip sees; they
had drifted to 0.3.0, 0.2.0 and 0.1.0. release-please now bumps all three."""
import json
import re
from pathlib import Path

import manifest_heal

ROOT = Path(__file__).resolve().parent.parent


def test_every_version_matches_the_release_manifest():
    released = json.loads((ROOT / ".release-please-manifest.json").read_text())["."]
    plugin = re.search(r"^version:\s*(\S+)\s*$", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
    project = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert (plugin, project, manifest_heal.VERSION) == (released, released, released)


def test_release_please_can_find_the_version_line():
    source = (ROOT / "manifest_heal.py").read_text()
    assert re.search(r'^VERSION = "[0-9.]+"  # x-release-please-version$', source, re.M)
