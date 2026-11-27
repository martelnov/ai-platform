"""ComfyUI GPU runtime, allocation and lifecycle tests."""

import unittest
from pathlib import Path
from unittest.mock import patch

from test_comfyui_workload import agent, work, CFG


GPU1 = "GPU-11111111-1111-1111-1111-111111111111"
GPU2 = "GPU-22222222-2222-2222-2222-222222222222"


def gpu_work(count=1):
    spec = work()
    spec.update({
        "mode": "gpu",
        "host_id": 1,
        "profile_name": f"gpu-{count}",
        "image": "ai-platform-comfyui:universal",
        "requires_reservation": True,
        "reservation_id": 10,
        "gpu_min": count,
        "gpu_max": count,
        "cpu_limit": float(4 * count),
        "memory_limit_bytes": 17179869184 * count,
        "profile_config": {
            "pids_limit": 256,
            "purpose": "generation",
        },
        "reservation": {
            "id": 10,
            "user_id": 1,
            "gpu_count": count,
            "status": "active",
            "valid_now": True,
        },
        "allocated_gpus": [
            {
                "id": i,
                "gpu_uuid": uuid,
                "host_id": 1,
                "enabled": True,
                "present": True,
            }
            for i, uuid in enumerate(
                [GPU1, GPU2][:count], start=1
            )
        ],
    })
    return spec


def docker_info(spec):
    return {
        "State": {"Running": True},
        "Config": {
            "Image": spec["image"],
            "User": "20000:20000",
            "Labels": {
                "ai.platform.component": "comfyui",
                "ai.platform.application": "comfyui",
                "ai.platform.user_id": "1",
                "ai.platform.connector_id": "1",
                "ai.platform.workload_id": "5",
            },
        },
        "HostConfig": {
            "NetworkMode": "container:ts-id",
            "DeviceRequests": [{
                "Driver": "nvidia",
                "Count": 0,
                "DeviceIDs": [
                    gpu["gpu_uuid"]
                    for gpu in spec["allocated_gpus"]
                ],
                "Capabilities": [["gpu"]],
            }],
        },
    }


class GPUValidationTests(unittest.TestCase):

    def setUp(self):
        config = patch.object(agent, "load_config", return_value=CFG)
        config.start()
        self.addCleanup(config.stop)

    def test_gpu_1(self):
        agent.validated_comfyui_spec(gpu_work(1), start=True)

    def test_gpu_2(self):
        agent.validated_comfyui_spec(gpu_work(2), start=True)

    def test_before_allocation(self):
        spec = gpu_work(2)
        spec["allocated_gpus"] = []
        agent.validated_comfyui_spec(
            spec, start=True, require_allocation=False
        )

    def test_missing_allocation(self):
        spec = gpu_work(1)
        spec["allocated_gpus"] = []
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)

    def test_expired_reservation(self):
        spec = gpu_work()
        spec["reservation"]["valid_now"] = False
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)

    def test_missing_reservation(self):
        spec = gpu_work()
        spec["reservation"] = None
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)

    def test_wrong_reservation_owner(self):
        spec = gpu_work()
        spec["reservation"]["user_id"] = 999
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)

    def test_insufficient_reservation(self):
        spec = gpu_work(2)
        spec["reservation"]["gpu_count"] = 1
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)

    def test_wrong_gpu_host(self):
        spec = gpu_work()
        spec["allocated_gpus"][0]["host_id"] = 2
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)

    def test_disabled_gpu(self):
        spec = gpu_work()
        spec["allocated_gpus"][0]["enabled"] = False
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)

    def test_missing_gpu(self):
        spec = gpu_work()
        spec["allocated_gpus"][0]["present"] = False
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)

    def test_duplicate_gpu(self):
        spec = gpu_work(2)
        spec["allocated_gpus"][1]["gpu_uuid"] = GPU1
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)

    def test_invalid_uuid(self):
        spec = gpu_work()
        spec["allocated_gpus"][0]["gpu_uuid"] = "all"
        with self.assertRaises(ValueError):
            agent.validated_comfyui_spec(spec, start=True)


class GPUDockerTests(unittest.TestCase):

    def docker_args(self, count):
        spec = gpu_work(count)
        roots = {
            "models": Path("/AI/models/users/test"),
            "data": Path("/AI/data/users/test"),
        }
        with patch.object(agent, "load_config", return_value=CFG), \
             patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "is_symlink", return_value=False), \
             patch.object(Path, "resolve",
                          return_value=Path("/AI/models/common")):
            return agent.comfyui_docker_args(
                spec, roots, roots["data"] / "apps/comfyui",
                1, 20000, 20000
            )

    def test_docker_one_gpu(self):
        args = self.docker_args(1)
        self.assertEqual(
            args[args.index("--gpus") + 1],
            '"device=' + GPU1 + '"'
        )
        self.assertIn("COMFYUI_MODE=gpu", args)
        self.assertIn("ai-platform-comfyui:universal", args)

    def test_docker_two_gpus(self):
        args = self.docker_args(2)
        self.assertEqual(
            args[args.index("--gpus") + 1],
            '"device=' + GPU1 + "," + GPU2 + '"'
        )
        self.assertNotIn("all", args)
        self.assertNotIn("--cpu", args)

    def test_valid_container_allocation(self):
        spec = gpu_work(2)
        agent.comfyui_validate_container_gpus(
            docker_info(spec), spec
        )

    def test_wrong_container_gpu(self):
        spec = gpu_work(1)
        info = docker_info(spec)
        info["HostConfig"]["DeviceRequests"][0]["DeviceIDs"] = [GPU2]
        with self.assertRaises(RuntimeError):
            agent.comfyui_validate_container_gpus(info, spec)

    def test_container_all_gpus_rejected(self):
        spec = gpu_work()
        info = docker_info(spec)
        info["HostConfig"]["DeviceRequests"][0]["Count"] = -1
        with self.assertRaises(RuntimeError):
            agent.comfyui_validate_container_gpus(info, spec)


class GPULifecycleTests(unittest.TestCase):

    def setUp(self):
        config = patch.object(agent, "load_config", return_value=CFG)
        config.start()
        self.addCleanup(config.stop)

    def test_stop_missing_container_releases_gpu(self):
        spec = gpu_work()
        with patch.object(agent, "inspect_container", return_value=None), \
             patch.object(agent, "helper_workload_command") as helper:
            result = agent.comfyui_workload_stop(spec)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            [c.args[0] for c in helper.call_args_list],
            ["workload-set-stopped", "workload-release-gpus"]
        )

    def test_stop_failure_retains_gpu(self):
        spec = gpu_work()
        with patch.object(agent, "inspect_container",
                          return_value=docker_info(spec)), \
             patch.object(agent, "run", side_effect=RuntimeError("Docker failure")), \
             patch.object(agent, "helper_workload_command") as helper:
            with self.assertRaises(RuntimeError):
                agent.comfyui_workload_stop(spec)

        commands = [c.args[0] for c in helper.call_args_list]
        self.assertNotIn("workload-release-gpus", commands)

    def test_expired_reservation_stops_container(self):
        spec = gpu_work()
        spec["status"] = "running"
        spec["reservation"]["valid_now"] = False

        with patch.object(agent, "comfyui_workload_stop") as stop:
            changed = agent.reconcile_comfyui_gpu(
                spec, docker_info(spec)
            )

        stop.assert_called_once_with(spec)
        self.assertEqual(changed, 1)

    def test_missing_container_releases_gpu(self):
        spec = gpu_work()
        spec["status"] = "running"

        with patch.object(agent, "inspect_container", return_value=None), \
             patch.object(agent, "helper_workload_command") as helper:
            changed = agent.reconcile_comfyui_gpu(spec, None)

        self.assertEqual(changed, 1)
        self.assertEqual(
            [c.args[0] for c in helper.call_args_list],
            ["workload-set-stopped", "workload-release-gpus"]
        )


if __name__ == "__main__":
    unittest.main()
