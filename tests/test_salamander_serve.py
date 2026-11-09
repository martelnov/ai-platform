"""Ensure Serve configuration extends, rather than replaces, HTTPS 443."""
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'host' / 'ai-platform-launcher-manager.py'
spec = importlib.util.spec_from_file_location('launch_mgr', PATH)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class ServeTests(unittest.TestCase):
    def test_existing_8443_is_preserved(self):
        config = {
            'TCP': {'443': {'HTTPS': True}, '8443': {'HTTPS': True}},
            'Web': {
                'ai-test.ts.net:443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8080'}}},
                'ai-test.ts.net:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8090'}}},
            },
        }
        with patch.object(manager, 'run', return_value=SimpleNamespace(
            stdout=json.dumps(config)
        )) as run:
            manager.ensure_salamander_serve('ai-ts-1')
        run.assert_called_once()

    def test_conflicting_8443_refused(self):
        config = {
            'TCP': {'443': {'HTTPS': True}, '8443': {'HTTPS': True}},
            'Web': {'ai-test.ts.net:8443': {'Handlers': {'/': {
                'Proxy': 'http://127.0.0.1:9999'}}}},
        }
        with patch.object(manager, 'run', return_value=SimpleNamespace(
            stdout=json.dumps(config)
        )) as run:
            with self.assertRaisesRegex(RuntimeError, 'unexpected proxy'):
                manager.ensure_salamander_serve('ai-ts-1')
        run.assert_called_once()

    def test_8443_setup_uses_separate_port(self):
        config = {'TCP': {'443': {'HTTPS': True}}, 'Web': {}}
        with patch.object(manager, 'run', return_value=SimpleNamespace(
            stdout=json.dumps(config)
        )) as run:
            manager.ensure_salamander_serve('ai-ts-1')
        self.assertEqual(run.call_count, 2)
        self.assertIn('--https=8443', run.call_args.args)
        self.assertNotIn('off', run.call_args.args)


if __name__ == '__main__':
    unittest.main()
