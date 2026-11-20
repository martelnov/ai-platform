"""Database-free tests of GPU admin mutations and their safety boundaries.

Run in the control-plane image, using unittest. They do not write to PostgreSQL.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / 'control-plane'
if not CONTROL.is_dir():
    CONTROL = ROOT
sys.path.insert(0, str(CONTROL))

from app import gpu_admin
from app.gpu_admin_write import GPURegistration, GPUEnabledChange, _peak_future

TEMPLATE_DIR = CONTROL / 'app' / 'templates'
A = '0000:08:00.0'
B = '0000:09:00.0'
ADMIN = b'admin@example.invalid'
TOKEN = 'signed-token-for-gpu-admin'


class FakeDatabase:
    def __init__(self):
        self.records = {
            1: {'id': 1, 'host_id': 1, 'pci_bus_id': B, 'enabled': True, 'present': True, 'retired_at': None},
        }
        self.booking_rows = []
        self.events = []
        self.commands = []
        self.next_id = 2

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.db.commands.append((sql, params))
        query = ' '.join(sql.upper().split())
        if 'PG_ADVISORY_XACT_LOCK' in query:
            self.rows = [(None,)]
        elif 'SELECT ID FROM HOSTS' in query:
            self.rows = [(1,)]
        elif 'SELECT ID FROM GPU_DEVICES WHERE HOST_ID' in query:
            host, bdf = params[:2]
            excluded = params[2] if len(params) == 3 else None
            self.rows = [(x['id'],) for x in self.db.records.values()
                         if x['host_id'] == host and x['pci_bus_id'].lower() == bdf
                         and x['retired_at'] is None and x['id'] != excluded]
        elif 'SELECT ID, ENABLED, PRESENT, PCI_BUS_ID, RETIRED_AT' in query:
            pk, host = params
            x = self.db.records.get(pk)
            self.rows = ([(x['id'], x['enabled'], x['present'], x['pci_bus_id'],
                           x['retired_at'])]
                         if x is not None and x['host_id'] == host else [])
        elif 'SELECT ID, ENABLED, PRESENT, RETIRED_AT' in query:
            pk, host = params
            x = self.db.records.get(pk)
            self.rows = ([(x['id'], x['enabled'], x['present'], x['retired_at'])]
                         if x is not None and x['host_id'] == host else [])
        elif 'SELECT COUNT(*) FROM GPU_DEVICES' in query:
            host, = params
            self.rows = [(sum(1 for x in self.db.records.values()
                              if x['host_id'] == host and x['enabled'] and x['present']),)]
        elif 'SELECT STARTS_AT, ENDS_AT, GPU_COUNT FROM RESERVATIONS' in query:
            self.rows = self.db.booking_rows
        elif 'INSERT INTO GPU_DEVICES' in query:
            host, _uuid, bdf, _name, _vram, _numa, _gen, _width = params
            pk = self.db.next_id
            self.db.next_id += 1
            self.db.records[pk] = {
                'id': pk, 'host_id': host, 'pci_bus_id': bdf,
                'enabled': False, 'present': True, 'retired_at': None,
            }
            self.rows = [(pk,)]
        elif 'UPDATE GPU_DEVICES SET ENABLED = TRUE' in query:
            self.db.records[params[0]].update(enabled=True, present=True)
        elif 'UPDATE GPU_DEVICES SET ENABLED = FALSE, RETIRED_AT = NOW()' in query:
            retired = datetime.now(timezone.utc)
            self.db.records[params[0]].update(enabled=False, retired_at=retired)
            self.rows = [(retired,)]
        elif 'UPDATE GPU_DEVICES SET ENABLED = FALSE, RETIRED_AT = NULL' in query:
            self.db.records[params[0]].update(enabled=False, retired_at=None)
        elif 'UPDATE GPU_DEVICES SET ENABLED = FALSE' in query:
            self.db.records[params[0]].update(enabled=False)
        elif 'INSERT INTO GPU_DEVICE_EVENTS' in query:
            self.db.events.append(tuple(params))
        else:
            raise AssertionError('Unexpected SQL: ' + sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class GPUAdminWriteTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.scans = []
        self.detected = [
            {'pci_bus_id': A, 'pci_name': 'Silicon Motion SM750',
             'numa_node': 0, 'pcie': {'max_link_speed': '2.5 GT/s PCIe',
                                     'max_link_width': 1}},
            {'pci_bus_id': B, 'pci_name': 'AMD Radeon RX 460',
             'numa_node': 0, 'pcie': {'max_link_speed': '8.0 GT/s PCIe',
                                     'max_link_width': 8},
             'amdgpu': {'vram_total_bytes': None}},
        ]

        def require_admin(req, conn):
            if req.headers.get('tailscale-user-login') != ADMIN.decode():
                raise HTTPException(403, 'Only administrators')
            return {'login': ADMIN.decode(), 'name': 'Admin'}

        def agent(payload, **_):
            self.scans.append(payload)
            return {'ok': True, 'result': {'devices': self.detected}}

        def csrf_token(login, action):
            self.assertEqual(login, ADMIN.decode())
            self.assertEqual(action, 'gpu-admin')
            return TOKEN

        self.router = gpu_admin.make_gpu_admin_router(
            lambda: self.db, require_admin, agent,
            Jinja2Templates(directory=str(TEMPLATE_DIR)),
            csrf_token,
        )

    def req(self, *, admin=True, csrf=True):
        headers = []
        if admin:
            headers.append((b'tailscale-user-login', ADMIN))
        if csrf:
            headers.append((b'x-ai-platform-csrf', TOKEN.encode()))
        return Request({'type': 'http', 'method': 'POST',
                        'path': '/admin/gpus/register', 'headers': headers})

    def endpoint(self, path):
        return next(r.endpoint for r in self.router.routes if r.path == path)

    def register(self, bdf=A, **kwargs):
        return self.endpoint('/admin/gpus/register')(
            self.req(**kwargs), GPURegistration(pci_bus_id=bdf))

    def enabled(self, pk=1, value=False, **kwargs):
        return self.endpoint('/admin/gpus/{gpu_id}/enabled')(
            self.req(**kwargs), pk, GPUEnabledChange(enabled=value))

    def test_requires_admin_before_any_scan_or_write(self):
        with self.assertRaises(HTTPException) as caught:
            self.register(admin=False)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.scans, [])
        self.assertEqual(self.db.commands, [])

    def test_requires_csrf_before_any_scan_or_write(self):
        for path in ('register', 'enabled'):
            with self.subTest(path=path), self.assertRaises(HTTPException) as caught:
                if path == 'register':
                    self.register(csrf=False)
                else:
                    self.enabled(csrf=False)
            self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.scans, [])
        self.assertEqual(self.db.commands, [])

    def test_registers_detected_adapter_as_disabled(self):
        response = self.register()
        self.assertEqual(response['id'], 2)
        self.assertEqual(response['pci_bus_id'], A)
        self.assertFalse(response['enabled'])
        self.assertTrue(response['present'])
        self.assertFalse(self.db.records[2]['enabled'])
        self.assertEqual(self.scans, [{'op': 'gpu_inventory_scan'}])
        self.assertTrue(any('PG_ADVISORY_XACT_LOCK' in x[0].upper()
                            for x in self.db.commands))
        ins = next(params for sql, params in self.db.commands
                   if 'INSERT INTO gpu_devices' in sql)
        self.assertTrue(ins[1].startswith('inventory:'))
        self.assertEqual(ins[6:8], (1, 1))

    def test_rejects_duplicate_pci_and_no_second_insert(self):
        with self.assertRaises(HTTPException) as caught:
            self.register(B)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(self.db.records), 1)

    def test_rejects_missing_or_invalid_pci(self):
        for bdf, expected in [(A.replace('08', '0a'), 409), ('oops', 422)]:
            with self.subTest(bdf=bdf), self.assertRaises(HTTPException) as caught:
                self.register(bdf)
            self.assertEqual(caught.exception.status_code, expected)
        self.assertEqual(len(self.db.records), 1)

    def test_disable_with_no_bookings(self):
        result = self.enabled()
        self.assertTrue(result['changed'])
        self.assertFalse(self.db.records[1]['enabled'])
        self.assertTrue(self.db.records[1]['present'])
        self.assertEqual(self.scans, [])

    def test_disable_rejected_if_future_bookings_would_overbook(self):
        now = datetime.now(timezone.utc)
        self.db.booking_rows = [(now + timedelta(days=1),
                                 now + timedelta(days=1, hours=2), 1)]
        with self.assertRaises(HTTPException) as caught:
            self.enabled()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self.db.records[1]['enabled'])

    def test_enable_requires_detected_hardware(self):
        self.db.records[1]['enabled'] = False
        self.detected = [x for x in self.detected if x['pci_bus_id'] != B]
        with self.assertRaises(HTTPException) as caught:
            self.enabled(value=True)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(self.db.records[1]['enabled'])

    def test_enable_after_fresh_scan_marks_present(self):
        self.db.records[1].update(enabled=False, present=False)
        result = self.enabled(value=True)
        self.assertTrue(result['enabled'])
        self.assertTrue(result['present'])
        self.assertEqual(self.db.records[1]['enabled'], True)
        self.assertEqual(self.scans, [{'op': 'gpu_inventory_scan'}])

    def test_disabling_ignores_old_and_cancelled_bookings(self):
        now = datetime.now(timezone.utc)
        self.db.booking_rows = [(now - timedelta(days=3), now - timedelta(days=1), 1)]
        result = self.enabled()
        self.assertFalse(result['enabled'])

    def test_capacity_peak_handles_adjacent_and_overlapping(self):
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=1)
        rows = [(start, start + timedelta(hours=1), 1),
                (start + timedelta(hours=1), start + timedelta(hours=2), 1)]
        self.assertEqual(_peak_future(rows, now), 1)
        rows.append((start + timedelta(minutes=30), start + timedelta(hours=3), 1))
        self.assertEqual(_peak_future(rows, now), 2)

    def test_page_includes_csrf_token_and_management_controls(self):
        response = self.endpoint('/admin/gpus')(self.req())
        html = response.body.decode('utf-8')
        self.assertIn('id="gpu-csrf" value="' + TOKEN + '"', html)
        self.assertIn('X-AI-Platform-CSRF', html)
        self.assertIn('managementCell(', html)
        self.assertNotIn('innerHTML', html)


if __name__ == '__main__':
    unittest.main()
