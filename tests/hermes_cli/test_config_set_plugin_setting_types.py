"""``hermes config set plugins.entries.<id>.settings.<key>`` follows the plugin's declared type.

A plugin manifest's ``config_schema`` is the schema for its settings the way ``DEFAULT_CONFIG`` is
for core keys: a ``type: str`` setting given ``off`` must stay the string the plugin compares
against, not the YAML boolean ``false``.
"""

import os
from unittest.mock import patch

import pytest

import hermes_yaml as yaml

PLUGIN_YAML = """\
name: dreamy
version: 0.1.0
description: probe
config_schema:
  dreaming_mode: {type: str}
  batch: {type: int}
"""


@pytest.fixture
def home(tmp_path):
    plugin = tmp_path / "plugins" / "dreamy"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
    (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


def _settings(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8-sig"))["plugins"]["entries"]["dreamy"]["settings"]


def test_declared_string_setting_keeps_the_word_and_numbers_still_coerce(home):
    from hermes_cli.config import set_config_value

    set_config_value("plugins.entries.dreamy.settings.dreaming_mode", "off")
    set_config_value("plugins.entries.dreamy.settings.batch", "5")
    set_config_value("plugins.entries.dreamy.settings.undeclared", "off")

    assert _settings(home) == {"dreaming_mode": "off", "batch": 5, "undeclared": False}
