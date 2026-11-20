"""Unit tests for non-destructive GPU retirement and restoration.

Mock database only: migration and real PostgreSQL writes are tested separately.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / 'control-plane'
if not CONTROL.is_dir():
    CONTROL = ROOT
sys.path.insert(0, str(CONTROL))

from app.gpu_admin_write import GPUEnabledChange, GPURetiredChange
import test_gpu_admin_write as harness_module


class GPUAdminRetirementTests(unittest.TestCase):
    def setUp(self):
        self.h = harness_module.GPUAdminWriteTests('test_registers_detected_adapter_as_disabled')
        self.h.setUp()
        self.db = self.h.db

    def change(self, gpu_id=1, retired=True, **request_kwargs):
        endpoint = self.h.endpoint('/admin/gpus/{gpu_id}/retired')
        return endpoint(self.h.req(**request_kwargs), gpu_id,
                        GPURetiredChange(retired=retired))

    def test_retire_preserves_registration_and_writes_audit(self):
        result = self.change()
        self.assertTrue(result['changed'])
        self.assertTrue(result['retired'])
        self.assertFalse(result['enabled'])
        self.assertIn(1, self.db.records)
        self.assertFalse(self.db.records[1]['enabled'])
        self.assertIsNotNone(self.db.records[1]['retired_at'])
        self.assertTrue(self.db.records[1]['present'])
        self.assertEqual(self.db.events, [(1, 'retire', 'admin@example.invalid')])
        self.assertEqual(self.h.scans, [])

    def test_retire_enabled_gpu_checks_future_capacity(self):
        now = datetime.now(timezone.utc)
        self.db.booking_rows = [(now + timedelta(minutes=5),
                                 now + timedelta(hours=2), 1)]
        with self.assertRaises(HTTPException) as caught:
            self.change()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self.db.records[1]['enabled'])
        self.assertIsNone(self.db.records[1]['retired_at'])
        self.assertEqual(self.db.events, [])

    def test_retire_enabled_gpu_with_spare_capacity(self):
        # A second enabled card keeps capacity >= 1 after retirement.
        self.db.records[2] = dict(id=2, host_id=1, pci_bus_id='0000:07:00.0',
                                  enabled=True, present=True, retired_at=None)
        now = datetime.now(timezone.utc)
        self.db.booking_rows = [(now + timedelta(minutes=1),
                                 now + timedelta(hours=1), 1)]
        result = self.change()
        self.assertTrue(result['retired'])
        self.assertFalse(self.db.records[1]['enabled'])
        self.assertTrue(self.db.records[2]['enabled'])

    def test_retire_disabled_gpu_does_not_reduce_capacity(self):
        self.db.records[1]['enabled'] = False
        now = datetime.now(timezone.utc)
        self.db.booking_rows = [(now + timedelta(hours=1),
                                 now + timedelta(hours=2), 2)]
        result = self.change()
        self.assertTrue(result['retired'])
        self.assertFalse(any('SELECT count(*) FROM gpu_devices' in sql
                             for sql, _ in self.db.commands))

    def test_retire_missing_device_needs_no_scan(self):
        self.db.records[1].update(enabled=False, present=False)
        result = self.change()
        self.assertTrue(result['retired'])
        self.assertFalse(self.db.records[1]['present'])
        self.assertEqual(self.h.scans, [])

    def test_repeated_retirement_does_not_write_another_audit(self):
        self.change()
        result = self.change()
        self.assertFalse(result['changed'])
        self.assertEqual(len(self.db.events), 1)

    def test_restore_keeps_gpu_disabled_and_remembers_actions(self):
        self.change()
        result = self.change(retired=False)
        self.assertTrue(result['changed'])
        self.assertFalse(result['retired'])
        self.assertFalse(self.db.records[1]['enabled'])
        self.assertIsNone(self.db.records[1]['retired_at'])
        self.assertEqual([e[1] for e in self.db.events], ['retire', 'restore'])

    def test_restore_missing_gpu_requires_no_scan(self):
        self.db.records[1].update(enabled=False, present=False)
        self.change()
        self.change(retired=False)
        self.assertFalse(self.db.records[1]['present'])
        self.assertFalse(self.db.records[1]['enabled'])
        self.assertEqual(self.h.scans, [])

    def test_repeated_restore_is_idempotent(self):
        result = self.change(retired=False)
        self.assertFalse(result['changed'])
        self.assertEqual(self.db.events, [])

    def test_cannot_enable_retired_gpu(self):
        self.change()
        with self.assertRaises(HTTPException) as caught:
            self.h.enabled(value=True)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.h.scans, [])
        self.assertFalse(self.db.records[1]['enabled'])

    def test_enable_after_restore_requires_scan(self):
        self.change()
        self.change(retired=False)
        result = self.h.enabled(value=True)
        self.assertTrue(result['enabled'])
        self.assertEqual(self.h.scans, [{'op': 'gpu_inventory_scan'}])

    def test_require_admin_before_any_write(self):
        for authorized, csrf in ((False, True), (True, False)):
            with self.subTest(admin=authorized, csrf=csrf):
                with self.assertRaises(HTTPException) as caught:
                    self.change(admin=authorized, csrf=csrf)
                self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.db.commands, [])
        self.assertEqual(self.db.events, [])

    def test_unknown_gpu_and_invalid_id(self):
        for gpu_id, expected in ((0, 422), (999, 404)):
            with self.subTest(gpu_id=gpu_id), self.assertRaises(HTTPException) as caught:
                self.change(gpu_id=gpu_id)
            self.assertEqual(caught.exception.status_code, expected)
        self.assertEqual(self.db.events, [])

    def test_bool_must_be_strict(self):
        for invalid in ('true', 1, None):
            with self.subTest(value=invalid), self.assertRaises(ValidationError):
                GPURetiredChange(retired=invalid)

    def test_retirement_holds_booking_advisory_lock_before_changes(self):
        self.change()
        commands = [sql.upper() for sql, _ in self.db.commands]
        lock_idx = next(i for i, sql in enumerate(commands) if 'PG_ADVISORY_XACT_LOCK' in sql)
        change_idx = next(i for i, sql in enumerate(commands) if 'UPDATE GPU_DEVICES' in sql)
        self.assertLess(lock_idx, change_idx)

    def test_inventory_serializes_retirement_and_ui(self):
        # Read-only inventory will expose 'retired_at' to the admin UI.
        source = Path(__file__).resolve().parents[1] / 'control-plane' / 'app' / 'gpu_admin.py'
        if not source.exists():
            source = Path(__file__).resolve().parents[1] / 'app' / 'gpu_admin.py'
        code = source.read_text()
        self.assertIn('enabled, present, last_seen_at, retired_at', code)
        self.assertIn('"last_seen_at", "retired_at"', code)
        template = source.parent / 'templates' / 'admin-gpus.html'
        html = template.read_text()
        self.assertIn('Obnovit (zakázanou)', html)
        self.assertIn("['retire', 'Vyřadit']", html)
        self.assertIn('/admin/gpus/${r.id}/retired', html)
        self.assertNotIn('innerHTML', html)

    def test_retired_slot_shown_once_without_claiming_physical_identity(self):
        from app.gpu_admin import reconcile_inventory
        detected = [{'pci_bus_id': '0000:09:00.0', 'pci_name': 'SM750'}]
        original = {'id': 1, 'pci_bus_id': '0000:09:00.0',
                    'name': 'SM750', 'retired_at': datetime.now(timezone.utc)}
        rows = reconcile_inventory(detected, [original])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['scan_status'], 'retired')
        self.assertEqual(rows[0]['match_basis'], 'retired_slot_provisional')
        self.assertEqual(rows[0]['registered']['id'], 1)
        self.assertIs(rows[0]['detected'], detected[0])

    def test_replaced_card_same_slot_requires_explicit_admin_choice(self):
        from app.gpu_admin import reconcile_inventory
        detected = [{'pci_bus_id': '0000:09:00.0', 'pci_name': 'Different GPU'}]
        original = {'id': 1, 'pci_bus_id': '0000:09:00.0',
                    'name': 'SM750', 'retired_at': datetime.now(timezone.utc)}
        rows = reconcile_inventory(detected, [original])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['registered']['name'], 'SM750')
        self.assertEqual(rows[0]['detected']['pci_name'], 'Different GPU')
        self.assertEqual(rows[0]['match_basis'], 'retired_slot_provisional')
        html = (Path(__file__).resolve().parents[1] / 'control-plane' / 'app' /
                'templates' / 'admin-gpus.html')
        if not html.exists():
            html = Path(__file__).resolve().parents[1] / 'app' / 'templates' / 'admin-gpus.html'
        page = html.read_text()
        self.assertIn("['register', 'Registrovat náhradu']", page)
        self.assertIn('Registruj pouze JINOU fyzickou kartu', page)

    def test_multiple_retired_in_one_slot_keep_prior_history(self):
        from app.gpu_admin import reconcile_inventory
        detected = [{'pci_bus_id': '0000:09:00.0', 'pci_name': 'Replacement'}]
        old = {'id': 1, 'pci_bus_id': '0000:09:00.0',
               'retired_at': datetime.now(timezone.utc)}
        recent = {'id': 3, 'pci_bus_id': '0000:09:00.0',
                  'retired_at': datetime.now(timezone.utc)}
        rows = reconcile_inventory(detected, [old, recent])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['registered']['id'], 3)
        self.assertEqual(rows[0]['scan_status'], 'retired')
        self.assertEqual(rows[1]['registered']['id'], 1)
        self.assertIsNone(rows[1]['detected'])

    def test_active_registration_takes_precedence_over_retired_history(self):
        from app.gpu_admin import reconcile_inventory
        detected = [{'pci_bus_id': '0000:09:00.0', 'pci_name': 'New GPU'}]
        old = {'id': 1, 'pci_bus_id': '0000:09:00.0',
               'retired_at': datetime.now(timezone.utc)}
        active = {'id': 2, 'pci_bus_id': '0000:09:00.0', 'retired_at': None}
        rows = reconcile_inventory(detected, [old, active])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['scan_status'], 'registered')
        self.assertEqual(rows[0]['registered']['id'], 2)
        self.assertEqual(rows[1]['scan_status'], 'retired')
        self.assertEqual(rows[1]['registered']['id'], 1)

    def test_register_replacement_in_retired_pci_slot(self):
        self.change()
        new = self.h.register('0000:09:00.0')
        self.assertEqual(new['id'], 2)
        self.assertNotEqual(self.db.records[1]['retired_at'], None)
        self.assertIsNone(self.db.records[2]['retired_at'])
        self.assertFalse(self.db.records[2]['enabled'])
        self.assertEqual(self.db.records[1]['pci_bus_id'],
                         self.db.records[2]['pci_bus_id'])

    def test_restore_refuses_pci_slot_occupied_by_replacement(self):
        self.change()
        self.h.register('0000:09:00.0')
        with self.assertRaises(HTTPException) as caught:
            self.change(retired=False)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNotNone(self.db.records[1]['retired_at'])
        self.assertEqual(self.db.events, [(1, 'retire', 'admin@example.invalid')])

    def test_restoration_after_replacement_retires_again(self):
        self.change()
        self.h.register('0000:09:00.0')
        self.change(gpu_id=2, retired=True)
        result = self.change(retired=False)
        self.assertFalse(result['retired'])
        self.assertIsNone(self.db.records[1]['retired_at'])
        self.assertIsNotNone(self.db.records[2]['retired_at'])
        self.assertFalse(self.db.records[1]['enabled'])

    def test_migration_has_constraint_and_audit_table(self):
        root = Path(__file__).resolve().parents[1]
        sql = (root / 'db' / 'migrations' / '015_gpu_retirement.sql').read_text()
        self.assertIn('CHECK (retired_at IS NULL OR enabled = false)', sql)
        self.assertIn('CREATE TABLE gpu_device_events', sql)
        self.assertIn('CREATE UNIQUE INDEX idx_gpu_devices_active_host_pci', sql)
        self.assertIn('WHERE retired_at IS NULL AND pci_bus_id IS NOT NULL', sql)
        self.assertIn('REFERENCES gpu_devices(id) ON DELETE RESTRICT', sql)
        self.assertIn('INSERT INTO schema_migrations (version) VALUES (15)', sql)


    def test_gpu_administration_czech_labels(self):
        root = Path(__file__).resolve().parents[1]
        html = root / 'control-plane' / 'app' / 'templates' / 'admin-gpus.html'
        if not html.is_file():
            html = root / 'app' / 'templates' / 'admin-gpus.html'
        page = html.read_text(encoding='utf-8')
        for label in ('<html lang="cs">', 'Správa GPU', 'Skenovat GPU',
                      'Podrobnosti o zařízení', 'Spustit diagnostiku',
                      'Registrovat náhradu', 'Obnovit (zakázanou)',
                      'Vyřadit', 'Hostitel:', 'Vyřazená'):
            with self.subTest(label=label):
                self.assertIn(label, page)
        self.assertNotIn('<h1>GPU administration</h1>', page)

    def test_gpu_administration_czech_keeps_safety_and_api(self):
        root = Path(__file__).resolve().parents[1]
        html = root / 'control-plane' / 'app' / 'templates' / 'admin-gpus.html'
        if not html.is_file():
            html = root / 'app' / 'templates' / 'admin-gpus.html'
        page = html.read_text(encoding='utf-8')
        for required in ('Registruj pouze JINOU fyzickou kartu',
                         'NEMUSÍ jít o původní fyzickou kartu',
                         'window.confirm', 'X-AI-Platform-CSRF',
                         '/admin/gpus/register', '/admin/gpus/inventory',
                         '/admin/gpus/diagnostics?', 'textContent'):
            with self.subTest(required=required):
                self.assertIn(required, page)
        self.assertNotIn('innerHTML', page)


if __name__ == '__main__':
    unittest.main()
