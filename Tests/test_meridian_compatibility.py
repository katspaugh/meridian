"""Branding must not break saved shortcuts or existing data overrides."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCES = Path(__file__).resolve().parents[1] / 'Sources'
sys.path.insert(0, str(SOURCES))
import agent_navigation


class MeridianCompatibilityTests(unittest.TestCase):
    def test_existing_shortcut_is_preserved(self):
        for command in ('command:muxteroid-view', 'command:meridian-view'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as home:
                path = Path(home) / 'keybindings.json'
                original = json.dumps({'bindings': [{'context': 'Chat', 'bindings': {
                    agent_navigation.CHORD: command}}]})
                path.write_text(original)
                with patch.dict(os.environ, CLAUDE_CONFIG_DIR=home):
                    self.assertFalse(agent_navigation.ensure_shortcut())
                self.assertEqual(path.read_text(), original)

    def test_new_shortcut_uses_meridian(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, CLAUDE_CONFIG_DIR=home):
                self.assertTrue(agent_navigation.ensure_shortcut())
            data = json.loads((Path(home) / 'keybindings.json').read_text())
            self.assertEqual(data['bindings'][0]['bindings'][agent_navigation.CHORD], 'command:meridian-view')

    def test_data_override_precedence_and_legacy_support(self):
        with tempfile.TemporaryDirectory() as home:
            legacy, current = Path(home) / 'legacy', Path(home) / 'current'
            env = dict(os.environ, PYTHONPATH=str(SOURCES), MUXTEROID_HOME=str(legacy))
            env.pop('MERIDIAN_HOME', None)
            for override, expected in ((None, legacy), (str(current), current)):
                if override:
                    env['MERIDIAN_HOME'] = override
                output = subprocess.check_output([sys.executable, '-c', 'import streams; print(streams.ROOT)'], env=env, text=True)
                self.assertEqual(Path(output.strip()), expected.resolve())


if __name__ == '__main__':
    unittest.main()
