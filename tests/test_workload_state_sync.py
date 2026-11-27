"""Workload state recovery tests; use mocks, never write PostgreSQL or Docker."""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

agent = module('ai_platform_agent_workload_sync_test', ROOT / 'host/ai-platform-agent.py')
helper = module('ai_platform_helper_workload_sync_test', ROOT / 'host/ai-platform-helper.py')

CFG = {'node': {'name': 'ailab'}}

def workload(status='running', app='salamander'):
    return {
        'id': 11, 'user_id': 1, 'connector_id': 1,
        'uid': 20000, 'gid': 20000, 'username': 'test',
        'host_name': 'ailab', 'mode': 'utility' if app == 'salamander' else 'preparation',
        'requires_reservation': False, 'gpu_min': 0, 'gpu_max': 0,
        'command': None, 'application_code': app,
        'image': 'ai-platform-salamander:0.10.0-e7dd497' if app == 'salamander' else 'ai-platform-comfyui:universal',
        'status': status,
    }

def container(spec, running):
    app = spec['application_code']
    labels = {
        'ai.platform.component': 'file-manager' if app == 'salamander' else 'comfyui',
        'ai.platform.application': app,
        'ai.platform.user_id': str(spec['user_id']),
        'ai.platform.connector_id': str(spec['connector_id']),
        'ai.platform.workload_id': str(spec['id']),
    }
    return {'Config': {'Labels': labels, 'Image': spec['image'], 'User': '20000:20000'},
            'State': {'Running': running}}

class ReconcileTests(unittest.TestCase):
    def execute(self, spec, info):
        with patch.object(agent, 'load_config', return_value=CFG), \
             patch.object(agent, 'workload_list_local', return_value=[11]), \
             patch.object(agent, 'load_workload', return_value=spec), \
             patch.object(agent, 'inspect_container', return_value=info), \
             patch.object(agent, 'helper_workload_command') as update, \
             contextlib.redirect_stdout(io.StringIO()):
            result = agent.workload_reconcile_once()
        return result, update

    def test_power_failure_marks_stopped(self):
        result, update = self.execute(workload(), None)
        self.assertEqual(result, 1)
        update.assert_called_once_with('workload-set-stopped', 11)

    def test_running_container_keeps_running(self):
        w = workload()
        result, update = self.execute(w, container(w, True))
        self.assertEqual(result, 0)
        update.assert_not_called()

    def test_stale_stopped_state_is_recovered_when_container_running(self):
        w = workload('stopped')
        result, update = self.execute(w, container(w, True))
        self.assertEqual(result, 1)
        update.assert_called_once_with('workload-set-running', 11)

    def test_stopping_state_is_reconciled(self):
        result, update = self.execute(workload('stopping'), None)
        self.assertEqual(result, 1)
        update.assert_called_once_with('workload-set-stopped', 11)

    def test_pending_without_container_remains_pending(self):
        result, update = self.execute(workload('pending'), None)
        self.assertEqual(result, 0)
        update.assert_not_called()

    def test_failed_without_container_remains_failed(self):
        result, update = self.execute(workload('failed'), None)
        self.assertEqual(result, 0)
        update.assert_not_called()

    def test_wrong_owner_is_not_adopted(self):
        w = workload('stopped')
        info = container(w, True)
        info['Config']['Labels']['ai.platform.user_id'] = '2'
        result, update = self.execute(w, info)
        self.assertEqual(result, 0)
        update.assert_not_called()

    def test_wrong_image_is_not_adopted(self):
        w = workload('stopped')
        info = container(w, True)
        info['Config']['Image'] = 'untrusted'
        result, update = self.execute(w, info)
        self.assertEqual(result, 0)
        update.assert_not_called()

    def test_wrong_uid_is_not_adopted(self):
        w = workload('stopped')
        info = container(w, True)
        info['Config']['User'] = '0:0'
        result, update = self.execute(w, info)
        self.assertEqual(result, 0)
        update.assert_not_called()

    def test_other_host_is_not_updated(self):
        w = workload('running'); w['host_name'] = 'another-host'
        result, update = self.execute(w, None)
        self.assertEqual(result, 0)
        update.assert_not_called()

    def test_docker_failure_keeps_db_untouched(self):
        with patch.object(agent, 'load_config', return_value=CFG), \
             patch.object(agent, 'workload_list_local', return_value=[11]), \
             patch.object(agent, 'load_workload', return_value=workload()), \
             patch.object(agent, 'inspect_container', side_effect=RuntimeError('Docker not ready')), \
             patch.object(agent, 'helper_workload_command') as update, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.workload_reconcile_once(), 0)
        update.assert_not_called()

    def test_comfyui_stopped(self):
        w = workload('running', app='comfyui')
        with patch.object(agent, 'validated_comfyui_spec', return_value=(1, 1, 20000, 20000, 'test')):
            result, update = self.execute(w, None)
        self.assertEqual(result, 1)
        update.assert_called_once_with('workload-set-stopped', 11)

    def test_comfyui_running(self):
        w = workload('stopped', app='comfyui')
        with patch.object(agent, 'validated_comfyui_spec', return_value=(1, 1, 20000, 20000, 'test')):
            result, update = self.execute(w, container(w, True))
        self.assertEqual(result, 1)
        update.assert_called_once_with('workload-set-running', 11)

    def test_list_ids_rejects_bad_json_types(self):
        for text in ('{}', '[true]', '[1, 1]', '["2"]'):
            with self.subTest(text=text), patch.object(agent, 'run', return_value=SimpleNamespace(stdout=text)):
                with self.assertRaises((RuntimeError, ValueError)):
                    agent.workload_list_local()

    def test_list_ids_ok(self):
        with patch.object(agent, 'run', return_value=SimpleNamespace(stdout='[1,2]')) as run:
            self.assertEqual(agent.workload_list_local(), [1,2])
        run.assert_called_once_with([agent.WORKLOAD_HELPER, 'workload-list-local'], timeout=30)

    def test_helper_query_is_scoped_to_configured_host(self):
        with patch.object(helper, 'load_config', return_value=CFG), \
             patch.object(helper, 'psql', return_value='[11]') as query, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            helper.workload_list_local()
        self.assertEqual(output.getvalue().strip(), '[11]')
        sql = query.call_args.args[0]
        self.assertIn("h.name = 'ailab'", sql)
        self.assertIn("a.code = 'salamander'", sql)
        self.assertIn("a.code = 'comfyui'", sql)

    def test_helper_rejects_unsafe_host(self):
        with patch.object(helper, 'load_config', return_value={'node': {'name': "x';drop"}}), \
             patch.object(helper, 'psql') as psql:
            with self.assertRaises(SystemExit):
                helper.workload_list_local()
        psql.assert_not_called()

    def test_unavailable_database_causes_retry_not_crash(self):
        class FakeEvent:
            def __init__(self): self.waited = False
            def is_set(self): return self.waited
            def wait(self, seconds): self.waited = True; return True
        with patch.object(agent, 'workload_reconcile_once', side_effect=RuntimeError('DB unavailable')) as scan, \
             contextlib.redirect_stdout(io.StringIO()):
            agent.workload_reconcile_loop(FakeEvent())
        scan.assert_called_once()

if __name__ == '__main__':
    unittest.main()
