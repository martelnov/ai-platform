"""Tests for the host-agent's restricted GPU scanner RPC bridge."""

import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'host' / 'gpu_agent_bridge.py'
spec = importlib.util.spec_from_file_location('gpu_agent_bridge', MODULE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class GPUAgentBridgeTests(unittest.TestCase):
    def _run(self, payload):
        with patch.object(bridge.Path, 'is_file', return_value=True), \
                patch.object(bridge.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps({'devices': []}), stderr=''
                )) as call:
            result = bridge.gpu_agent_operation(payload)
        return result, call.call_args.args[0]

    def test_scan_uses_fixed_command(self):
        result, argv = self._run({'op': 'gpu_inventory_scan'})
        self.assertTrue(result['ok'])
        self.assertEqual(argv[-2:], ['scan', '--json'])
        self.assertEqual(result['result'], {'devices': []})

    def test_diagnostics_uses_selected_pci_address_and_tool(self):
        _, argv = self._run({'op': 'gpu_diagnostics',
                             'pci_bus_id': '0000:09:00.0', 'tool': 'lspci'})
        self.assertEqual(argv[-4:], ['diagnose', '0000:09:00.0', '--tool', 'lspci'])

    def test_rejects_non_bdf_and_command_injection(self):
        for value in ('09:00.0', '0000:09:00.0;touch /tmp/a',
                      '0000:09:00.0\n', '../../evil', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.gpu_agent_operation({'op': 'gpu_diagnostics', 'pci_bus_id': value})

    def test_rejects_unlisted_tool(self):
        with self.assertRaises(ValueError):
            bridge.gpu_agent_operation({'op': 'gpu_diagnostics',
                                        'pci_bus_id': '0000:09:00.0',
                                        'tool': 'bash'})

    def test_rejects_unknown_operation(self):
        with self.assertRaises(ValueError):
            bridge.gpu_agent_operation({'op': 'execute', 'command': 'id'})

    def test_rejects_missing_scanner(self):
        with patch.object(bridge.Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'not installed'):
                bridge.gpu_agent_operation({'op': 'gpu_inventory_scan'})

    def test_rejects_invalid_json(self):
        with patch.object(bridge.Path, 'is_file', return_value=True), \
                patch.object(bridge.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout='not json', stderr=''
                )):
            with self.assertRaisesRegex(RuntimeError, 'invalid GPU scanner output'):
                bridge.gpu_agent_operation({'op': 'gpu_inventory_scan'})

    def test_rejects_oversized_output(self):
        with patch.object(bridge.Path, 'is_file', return_value=True), \
                patch.object(bridge.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout=' ' * (bridge._MAX_OUTPUT + 1), stderr=''
                )):
            with self.assertRaisesRegex(RuntimeError, 'too large'):
                bridge.gpu_agent_operation({'op': 'gpu_inventory_scan'})


if __name__ == '__main__':
    unittest.main()
