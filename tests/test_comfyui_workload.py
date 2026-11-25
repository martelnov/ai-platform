"""ComfyUI CPU host-agent lifecycle and confinement checks (no Docker/DB)."""
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'host' / 'ai-platform-agent.py'
s = importlib.util.spec_from_file_location('ai_platform_agent_comfy_test', PATH)
agent = importlib.util.module_from_spec(s)
s.loader.exec_module(agent)


def work():
    return {
        'id': 5, 'host_name': 'ailab', 'mode': 'preparation',
        'reservation_id': None, 'user_id': 1, 'connector_id': 1,
        'uid': 20000, 'gid': 20000, 'username': 'test',
        'user_enabled': True, 'connector_enabled': True,
        'application_enabled': True, 'profile_enabled': True,
        'provisioning_status': 'ready', 'connector_status': 'running',
        'application_code': 'comfyui', 'profile_name': 'cpu-test',
        'requires_reservation': False, 'gpu_min': 0, 'gpu_max': 0,
        'command': None, 'image': 'ai-platform-comfyui:cpu-test',
        'application_config': {
            'kind': 'interactive-ai', 'start_policy': 'on_demand',
            'single_instance_per_user': True, 'cpu_only': True,
            'workdir': '/workspace', 'models_user': '/models/user',
            'models_common': '/models/common', 'models_common_read_only': True,
        },
        'environment': {'HOME': '/workspace', 'XDG_CACHE_HOME': '/workspace/.cache'},
        'profile_config': {'cpu_pool': 'interactive',
                           'memory_swap_bytes': 17179869184, 'pids_limit': 256,
                           'purpose': 'integration-test'},
        'memory_limit_bytes': 17179869184, 'cpu_limit': 4.0,
        'mounts': [], 'encryption_roots': [], 'status': 'pending',
    }

CFG = {'node': {'name': 'ailab'}, 'paths': {'models_root': '/AI/models'}}


class ComfyRuntimeTests(unittest.TestCase):
    @patch.object(agent, 'load_config', return_value=CFG)
    def test_valid_spec(self, _):
        self.assertEqual(agent.validated_comfyui_spec(work(), start=True),
                         (1, 1, 20000, 20000, 'test'))

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_reject_wrong_mode(self, _):
        w = work(); w['mode'] = 'gpu'
        with self.assertRaisesRegex(ValueError, 'preparation'):
            agent.validated_comfyui_spec(w)

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_reject_gpu(self, _):
        w = work(); w['gpu_max'] = 2
        with self.assertRaisesRegex(ValueError, 'GPU'):
            agent.validated_comfyui_spec(w)

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_reject_custom_command(self, _):
        w = work(); w['command'] = ['sh', '-c', 'id']
        with self.assertRaisesRegex(ValueError, 'command'):
            agent.validated_comfyui_spec(w)

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_reject_image(self, _):
        w = work(); w['image'] = 'evil:latest'
        with self.assertRaisesRegex(ValueError, 'image'):
            agent.validated_comfyui_spec(w)

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_reject_env(self, _):
        w = work(); w['environment']['PYTHONPATH'] = '/tmp'
        with self.assertRaisesRegex(ValueError, 'environment'):
            agent.validated_comfyui_spec(w)

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_reject_reservation(self, _):
        w = work(); w['reservation_id'] = 3
        with self.assertRaisesRegex(ValueError, 'reservation'):
            agent.validated_comfyui_spec(w)

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_reject_encrypted_models(self, _):
        w = work(); w['encryption_roots'] = [{'tree': 'models', 'root_path': '/'}]
        with self.assertRaisesRegex(ValueError, 'encrypted'):
            agent.validated_comfyui_spec(w)

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_reject_disabled_identity(self, _):
        w = work(); w['user_enabled'] = False
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            agent.validated_comfyui_spec(w, start=True)

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_reject_host(self, _):
        w = work(); w['host_name'] = 'other'
        with self.assertRaisesRegex(ValueError, 'host'):
            agent.validated_comfyui_spec(w)

    def test_name(self):
        self.assertEqual(agent.comfyui_container_name(work()), 'ai-comfy-1')

    @patch.object(agent, 'load_config', return_value=CFG)
    def test_docker_command_uses_only_expected_mounts(self, _):
        w = work()
        roots = {'models': Path('/AI/models/users/test'), 'data': Path('/AI/data/users/test')}
        with patch.object(Path, 'is_dir', return_value=True), \
             patch.object(Path, 'is_symlink', return_value=False), \
             patch.object(Path, 'resolve', side_effect=lambda *a, **k: a[0] if a else Path('/AI/models/common')):
            # pathlib.resolve is tested on real paths separately in integration.
            args = agent.comfyui_docker_args(w, roots, roots['data'] / 'apps/comfyui', 1, 20000, 20000)
        joined = ' '.join(args)
        self.assertIn('container:ai-ts-1', args)
        self.assertIn('20000:20000', args)
        self.assertIn('src=/AI/models/common,dst=/models/common,readonly', joined)
        self.assertIn('src=/AI/models/users/test,dst=/models/user', joined)
        self.assertIn('src=/AI/data/users/test/apps/comfyui,dst=/workspace', joined)
        self.assertIn('ai-platform-comfyui:cpu-test', args)
        self.assertEqual(args[args.index('--listen') + 1], '127.0.0.1')
        self.assertNotIn('--gpus', args)

    def test_container_wrong_owner_rejected(self):
        w = work()
        info = {'Config': {'Image': w['image'], 'User': '20000:20000',
                           'Labels': {'ai.platform.application': 'comfyui'}}}
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            agent.comfyui_container_validate(info, w)

    def test_status_stopped(self):
        with patch.object(agent, 'load_workload', return_value=work()), \
             patch.object(agent, 'load_config', return_value=CFG), \
             patch.object(agent, 'inspect_container', return_value=None):
            out = agent.workload_status({'workload_id': 5})
        self.assertEqual(out['status'], 'stopped')

    def test_stop_missing_marks_db(self):
        with patch.object(agent, 'load_workload', return_value=work()), \
             patch.object(agent, 'load_config', return_value=CFG), \
             patch.object(agent, 'inspect_container', return_value=None), \
             patch.object(agent, 'helper_workload_command') as helper:
            out = agent.workload_stop({'workload_id': 5})
        self.assertEqual(out['status'], 'stopped')
        helper.assert_called_once_with('workload-set-stopped', 5)

    def test_start_marks_workload(self):
        w = work()
        with patch.object(agent, 'load_workload', return_value=w), \
             patch.object(agent, 'load_config', return_value=CFG), \
             patch.object(agent, 'workload_storage_preflight', return_value={
                 'data': Path('/AI/data/users/test'), 'models': Path('/AI/models/users/test')}), \
             patch.object(agent, 'inspect_container', side_effect=[{'Id': 'ts-id', 'State': {'Running': True}}, None]), \
             patch.object(agent, 'tailscale_status', return_value={'BackendState': 'Running'}), \
             patch.object(agent, 'comfyui_prepare_workspace', return_value=Path('/AI/data/users/test/apps/comfyui')), \
             patch.object(agent, 'comfyui_docker_args', return_value=['docker', 'run', '-d', 'ok']), \
             patch.object(agent, 'container_running', return_value=True), \
             patch.object(agent, 'helper_workload_command') as helper, \
             patch.object(agent, 'run'):
            out = agent.workload_start({'workload_id': 5})
        self.assertEqual(out['status'], 'running')
        self.assertEqual([c.args[0] for c in helper.call_args_list],
                         ['workload-set-starting', 'workload-set-running'])


if __name__ == '__main__':
    unittest.main()


class ResourceAffinityTests(unittest.TestCase):
    def test_cpuset_is_passed_to_docker(self):
        spec = work()
        spec["cpuset_cpus"] = "0-1,16-17"
        roots = {
            "models": Path("/AI/models/users/test"),
            "data": Path("/AI/data/users/test"),
        }
        with patch.object(agent, "load_config", return_value=CFG), \
             patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "is_symlink", return_value=False), \
             patch.object(Path, "resolve",
                          side_effect=lambda *a, **k: Path("/AI/models/common")), \
             patch.object(Path, "read_text", return_value="0-31"):
            args = agent.comfyui_docker_args(
                spec, roots, roots["data"] / "apps/comfyui",
                1, 20000, 20000
            )

        self.assertEqual(args[args.index("--cpuset-cpus") + 1], "0-1,16-17")
        self.assertEqual(args[args.index("--cpus") + 1], "4.0")
        self.assertEqual(args[args.index("--memory") + 1], "17179869184")
        self.assertEqual(args[args.index("--memory-swap") + 1],
                         args[args.index("--memory") + 1])

    def test_invalid_cpuset_is_rejected(self):
        with patch.object(Path, "read_text", return_value="0-31"):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                agent.docker_cpuset_args({"cpuset_cpus": "0-32"})
