"""Regression tests for the read-only host-side GPU scanner."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'host' / 'gpu_inventory_scan.py'
spec = importlib.util.spec_from_file_location('gpu_inventory_scan', MODULE)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class GPUInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_device(self, bdf, class_code, vendor, device_id):
        device = self.root / bdf
        device.mkdir()
        (device / 'class').write_text(class_code)
        (device / 'vendor').write_text(vendor)
        (device / 'device').write_text(device_id)
        return device

    def test_scans_all_graphics_classes_and_ignores_other_devices(self):
        self.make_device('0000:08:00.0', '0x030000', '0x126f', '0x0750')
        self.make_device('0000:09:00.0', '0x030000', '0x1002', '0x67ef')
        self.make_device('0000:0a:00.0', '0x030200', '0x10de', '0x2204')
        self.make_device('0000:0b:00.0', '0x020000', '0x8086', '0x100e')
        devices = scanner.scan_devices(self.root, lookup_names=False)
        self.assertEqual([d['pci_bus_id'] for d in devices],
                         ['0000:08:00.0', '0000:09:00.0', '0000:0a:00.0'])
        self.assertEqual(devices[1]['vendor_id'], '0x1002')
        self.assertFalse(devices[1]['driver_bound'])

    def test_hardware_details_and_missing_data_are_robust(self):
        device = self.make_device('0000:09:00.0', '0x030000', '0x1002', '0x67ef')
        (device / 'current_link_speed').write_text('2.5 GT/s\n')
        (device / 'current_link_width').write_text('1\n')
        (device / 'max_link_speed').write_text('8.0 GT/s\n')
        (device / 'max_link_width').write_text('8\n')
        (device / 'numa_node').write_text('0\n')
        (device / 'mem_info_vram_total').write_text('4294967296\n')
        (device / 'gpu_busy_percent').write_text('30\n')
        drivers = self.root / 'drivers' / 'amdgpu'
        drivers.mkdir(parents=True)
        (device / 'driver').symlink_to(drivers)
        hw = device / 'hwmon' / 'hwmon0'
        hw.mkdir(parents=True)
        (hw / 'name').write_text('amdgpu')
        (hw / 'temp1_input').write_text('42000')
        found = scanner.scan_devices(self.root, lookup_names=False)[0]
        self.assertEqual(found['pcie']['current_link_width'], 1)
        self.assertEqual(found['pcie']['max_link_width'], 8)
        self.assertEqual(found['driver_in_use'], 'amdgpu')
        self.assertEqual(found['amdgpu']['vram_total_bytes'], 4294967296)
        self.assertEqual(found['hwmon'][0]['readings']['temp1_input'], '42000')

    def test_empty_sysfs(self):
        self.assertEqual(scanner.scan_devices(self.root), [])

    def test_status_reconciliation(self):
        scanned = [{'pci_bus_id': '0000:08:00.0'},
                   {'pci_bus_id': '0000:09:00.0'}]
        registered = [{'pci_bus_id': '0000:09:00.0', 'id': 1},
                      {'pci_bus_id': '0000:0c:00.0', 'id': 2}]
        merged = scanner.reconcile_inventory(scanned, registered)
        self.assertEqual([r['scan_status'] for r in merged],
                         ['new', 'registered', 'missing'])

    def test_invalid_or_non_graphics_bdf_rejected(self):
        self.make_device('0000:09:00.0', '0x030000', '0x1002', '0x67ef')
        with self.assertRaises(ValueError):
            scanner.run_diagnostics('0000:09:00.0;touch /tmp/pwn', sysfs_root=self.root)
        with self.assertRaises(ValueError):
            scanner.run_diagnostics('0000:01:00.0', sysfs_root=self.root)

    def test_auto_diagnostics_uses_fixed_readonly_commands(self):
        self.make_device('0000:09:00.0', '0x030000', '0x1002', '0x67ef')
        with patch.object(scanner, 'run_readonly', return_value={'status': 'unavailable'}) as run:
            result = scanner.run_diagnostics('0000:09:00.0', sysfs_root=self.root)
        self.assertEqual([entry['tool'] for entry in result['results']],
                         ['lspci', 'amd-smi', 'amd-smi', 'rocm-smi'])
        self.assertEqual(run.call_args_list[0].args[0],
                         ['lspci', '-Dvvk', '-s', '0000:09:00.0'])
        self.assertEqual(run.call_args_list[1].args[0],
                         ['amd-smi', 'static', '-g', '0000:09:00.0'])
        self.assertEqual(result['results'][-1]['scope'], 'all_supported_gpus')

    def test_allowlist_rejects_other_commands(self):
        with self.assertRaises(ValueError):
            scanner.run_readonly(['bash', '-c', 'echo unsafe'])

    def test_missing_tool_reported(self):
        with patch.object(scanner.shutil, 'which', return_value=None):
            result = scanner.run_readonly(['amd-smi', 'static', '-g', '0000:09:00.0'])
        self.assertEqual(result['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
