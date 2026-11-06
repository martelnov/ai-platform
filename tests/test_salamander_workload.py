"""Local unit checks for the first SalamanderRS workload runtime stage."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, call

PATH = Path(__file__).parents[1] / 'host' / 'ai-platform-agent.py'
spec = importlib.util.spec_from_file_location('ai_platform_agent_test', PATH)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def workload():
    return {
        'id': 4, 'host_name': 'ailab', 'mode': 'utility',
        'user_id': 1, 'connector_id': 1, 'uid': 20000, 'gid': 20000,
        'username': 'test', 'user_enabled': True, 'connector_enabled': True,
        'application_enabled': True, 'profile_enabled': True,
        'provisioning_status': 'ready', 'connector_status': 'running',
        'application_code': 'salamander', 'requires_reservation': False,
        'gpu_min': 0, 'gpu_max': 0, 'command': None,
        'image': 'ai-platform-salamander:0.10.0-7804126',
        'application_config': {'include_common': False},
        'environment': {
            'SALAMANDER_DATA_ROOT': '/data',
            'SALAMANDER_MODELS_ROOT': '/models',
            'SALAMANDER_ENABLE_WRITES': '1',
            'SALAMANDER_PORT': '8090',
        },
        'profile_config': {'cpu_pool': 'interactive',
                           'memory_swap_bytes': 536870912, 'pids_limit': 128},
        'memory_limit_bytes': 536870912, 'cpu_limit': 1.0,
        'mounts': [], 'encryption_roots': [],
    }


class WorkloadTests(unittest.TestCase):
    @patch.object(agent, 'load_config', return_value={'node': {'name': 'ailab'}})
    def test_salamander_validation(self, _):
        self.assertEqual(agent.validated_salamander_spec(workload(), start=True),
                         (1, 1, 20000, 20000, 'test'))

    @patch.object(agent, 'load_config', return_value={'node': {'name': 'ailab'}})
    def test_reject_gpu(self, _):
        w = workload()
        w['gpu_max'] = 1
        with self.assertRaisesRegex(ValueError, 'GPU'):
            agent.validated_salamander_spec(w, start=True)

    @patch.object(agent, 'load_config', return_value={'node': {'name': 'ailab'}})
    def test_reject_disabled_user(self, _):
        w = workload()
        w['user_enabled'] = False
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            agent.validated_salamander_spec(w, start=True)

    @patch.object(agent, 'load_config', return_value={'node': {'name': 'ailab'}})
    def test_reject_wrong_host(self, _):
        w = workload()
        w['host_name'] = 'other-host'
        with self.assertRaisesRegex(ValueError, 'another host'):
            agent.validated_salamander_spec(w, start=True)

    @patch.object(agent, 'load_config', return_value={'node': {'name': 'ailab'}})
    def test_docker_command_is_constrained(self, _):
        w = workload()
        roots = {'data': Path('/AI/data/users/test'),
                 'models': Path('/AI/models/users/test')}
        args = agent.salamander_docker_args(w, roots, 1, 20000, 20000)
        self.assertIn('container:ai-ts-1', args)
        self.assertIn('20000:20000', args)
        self.assertIn('type=bind,src=/AI/data/users/test,dst=/data', args)
        self.assertIn('type=bind,src=/AI/models/users/test,dst=/models', args)
        self.assertNotIn('/AI/models/common', ' '.join(args))
        self.assertIn('--memory-swap', args)
        self.assertIn('--cap-drop', args)
        self.assertEqual(args[-1], w['image'])

    def test_docker_command_rejects_common(self):
        w = workload()
        w['application_config']['include_common'] = True
        with self.assertRaisesRegex(ValueError, 'COMMON'):
            agent.salamander_docker_args(w, {}, 1, 20000, 20000)

    def test_docker_command_rejects_extra_env(self):
        w = workload()
        w['environment']['FOO'] = 'bar'
        with self.assertRaisesRegex(ValueError, 'environment'):
            agent.salamander_docker_args(w, {}, 1, 20000, 20000)

    def test_container_must_have_correct_owner_labels(self):
        w = workload()
        info = {'Config': {'Labels': {'ai.platform.component': 'file-manager',
                                      'ai.platform.application': 'salamander',
                                      'ai.platform.user_id': '2',
                                      'ai.platform.connector_id': '1',
                                      'ai.platform.workload_id': '4'}}}
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            agent.validate_salamander_container(info, w)

    @patch.object(agent, 'load_workload')
    @patch.object(agent, 'validated_salamander_spec')
    @patch.object(agent, 'inspect_container', return_value=None)
    def test_status_when_missing(self, _, __, load):
        w = workload()
        w['status'] = 'pending'
        load.return_value = w
        self.assertEqual(agent.workload_status({'workload_id': 4})['status'], 'stopped')

    @patch.object(agent, 'load_workload')
    @patch.object(agent, 'validated_salamander_spec')
    @patch.object(agent, 'inspect_container', return_value=None)
    @patch.object(agent, 'helper_workload_command')
    def test_stop_when_missing(self, mark, _, __, load):
        load.return_value = workload()
        self.assertEqual(agent.workload_stop({'workload_id': 4})['status'], 'stopped')
        mark.assert_called_once_with('workload-set-stopped', 4)

    @patch.object(agent, 'run', return_value=SimpleNamespace(
        returncode=1, stdout='', stderr='Cannot connect to the Docker daemon'
    ))
    def test_docker_failure_is_not_mistaken_for_absent_container(self, _):
        with self.assertRaisesRegex(RuntimeError, 'Docker daemon'):
            agent.inspect_container('ai-files-1')

    @patch.object(agent, 'load_workload', return_value=workload())
    @patch.object(agent, 'validated_salamander_spec', return_value=(
        1, 1, 20000, 20000, 'test'
    ))
    @patch.object(agent, 'workload_storage_preflight', return_value={
        'data': Path('/AI/data/users/test'),
        'models': Path('/AI/models/users/test'),
    })
    @patch.object(agent, 'inspect_container', side_effect=[
        {'State': {'Running': True}, 'Id': 'ts-container-id'}, None
    ])
    @patch.object(agent, 'tailscale_status', return_value={
        'BackendState': 'Running'
    })
    @patch.object(agent, 'run')
    @patch.object(agent, 'container_running', return_value=True)
    @patch.object(agent, 'helper_workload_command')
    def test_new_start_records_lifecycle(self, mark, _running, run, _ts,
                                         _inspect, _preflight, _validate, _load):
        result = agent.workload_start({'workload_id': 4})
        self.assertTrue(result['ok'])
        self.assertFalse(result['existing'])
        self.assertEqual([item.args[0] for item in mark.call_args_list],
                         ['workload-set-starting', 'workload-set-running'])
        self.assertTrue(any(c.args[0][:2] == ['docker', 'run']
                            for c in run.call_args_list))

    @patch.object(agent, 'load_workload', return_value=workload())
    @patch.object(agent, 'validated_salamander_spec', return_value=(
        1, 1, 20000, 20000, 'test'
    ))
    @patch.object(agent, 'workload_storage_preflight', return_value={
        'data': Path('/AI/data/users/test'),
        'models': Path('/AI/models/users/test'),
    })
    @patch.object(agent, 'inspect_container', side_effect=[
        {'State': {'Running': True}, 'Id': 'ts-container-id'},
        {'State': {'Running': True},
         'HostConfig': {'NetworkMode': 'container:ts-container-id'},
         'Config': {
             'Image': 'ai-platform-salamander:0.10.0-7804126',
             'User': '20000:20000',
             'Labels': {
                 'ai.platform.component': 'file-manager',
                 'ai.platform.application': 'salamander',
                 'ai.platform.user_id': '1',
                 'ai.platform.connector_id': '1',
                 'ai.platform.workload_id': '4',
             }
         }}
    ])
    @patch.object(agent, 'tailscale_status', return_value={
        'BackendState': 'Running'
    })
    @patch.object(agent, 'run')
    @patch.object(agent, 'helper_workload_command')
    def test_running_start_is_idempotent(self, mark, run, _ts,
                                        _inspect, _preflight, _validate, _load):
        result = agent.workload_start({'workload_id': 4})
        self.assertTrue(result['existing'])
        mark.assert_called_once_with('workload-set-running', 4)
        self.assertFalse(any(c.args[0][:2] == ['docker', 'run']
                             for c in run.call_args_list))

    @patch.object(agent, 'load_config', return_value={
        'node': {'name': 'ailab'},
        'paths': {'data_root': '/AI/data', 'models_root': '/AI/models'},
    })
    @patch.object(agent.os.path, 'ismount', return_value=True)
    @patch.object(agent, 'resolve_user_storage_root')
    def test_storage_preflight_ownership(self, roots, _mount, _):
        roots.return_value = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=30000, st_gid=30000))
        with self.assertRaisesRegex(RuntimeError, 'UID/GID'):
            agent.workload_storage_preflight(workload())


if __name__ == '__main__':
    unittest.main()
