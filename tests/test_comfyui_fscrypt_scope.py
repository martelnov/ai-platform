"""ComfyUI should not depend on fscrypt keys for unrelated private folders."""
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ai_platform_agent_fscrypt_scope_test", ROOT / "host/ai-platform-agent.py"
)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class FakeRoot:
    def stat(self):
        return SimpleNamespace(st_uid=20000, st_gid=20000)


class ComfyFscryptScopeTests(unittest.TestCase):
    def test_comfyui_ignores_unrelated_private_roots(self):
        spec = {"application_code": "comfyui"}
        for root in ("/private", "/private2", "/other/apps", "/apps2"):
            with self.subTest(root=root):
                self.assertFalse(agent.workload_fscrypt_root_relevant(
                    spec, {"tree": "data", "root_path": root}
                ))

    def test_workspace_and_ancestors_are_relevant(self):
        spec = {"application_code": "comfyui"}
        for root in ("/", "/apps", "/apps/comfyui",
                     "/apps/comfyui/input", "/apps/comfyui/user/private"):
            with self.subTest(root=root):
                self.assertTrue(agent.workload_fscrypt_root_relevant(
                    spec, {"tree": "data", "root_path": root}
                ))

    def test_malformed_paths_fail_closed(self):
        spec = {"application_code": "comfyui"}
        for root in (None, "private", "/apps//comfyui", "/private/../apps",
                     "/apps/./comfyui", "/apps/comfyui/", "/private/\x00"):
            with self.subTest(root=root):
                self.assertTrue(agent.workload_fscrypt_root_relevant(
                    spec, {"tree": "data", "root_path": root}
                ))

    def test_models_and_salamander_remain_restricted(self):
        self.assertTrue(agent.workload_fscrypt_root_relevant(
            {"application_code": "comfyui"},
            {"tree": "models", "root_path": "/private"}
        ))
        self.assertTrue(agent.workload_fscrypt_root_relevant(
            {"application_code": "salamander"},
            {"tree": "data", "root_path": "/private"}
        ))

    def test_actual_preflight_ignores_locked_unrelated_data(self):
        spec = {"application_code": "comfyui", "mounts": [],
                "encryption_roots": [
                    {"tree": "data", "root_path": "/private"},
                    {"tree": "data", "root_path": "/private2"},
                ]}
        validator = lambda _spec, start=False: (1, 1, 20000, 20000, "test")
        with patch.object(agent, "load_config", return_value={
                "paths": {"data_root": "/AI/data", "models_root": "/AI/models"}}), \
             patch.object(agent.os.path, "ismount", return_value=True), \
             patch.object(agent, "resolve_user_storage_root", return_value=FakeRoot()), \
             patch.object(agent, "user_encryption_status") as status:
            roots = agent.workload_storage_preflight(spec, validator=validator)
        self.assertIn("data", roots)
        self.assertIn("models", roots)
        status.assert_not_called()

    def test_actual_preflight_rejects_locked_workspace(self):
        spec = {"application_code": "comfyui", "mounts": [],
                "encryption_roots": [
                    {"tree": "data", "root_path": "/apps"},
                ]}
        validator = lambda _spec, start=False: (1, 1, 20000, 20000, "test")
        with patch.object(agent, "load_config", return_value={
                "paths": {"data_root": "/AI/data", "models_root": "/AI/models"}}), \
             patch.object(agent.os.path, "ismount", return_value=True), \
             patch.object(agent, "resolve_user_storage_root", return_value=FakeRoot()), \
             patch.object(agent, "user_encryption_status", return_value={"state": "locked"}):
            with self.assertRaisesRegex(RuntimeError, "fscrypt locked: data:/apps"):
                agent.workload_storage_preflight(spec, validator=validator)

    def test_salamander_still_rejects_any_locked_data(self):
        spec = {"application_code": "salamander", "mounts": [],
                "encryption_roots": [
                    {"tree": "data", "root_path": "/private"},
                ]}
        validator = lambda _spec, start=False: (1, 1, 20000, 20000, "test")
        with patch.object(agent, "load_config", return_value={
                "paths": {"data_root": "/AI/data", "models_root": "/AI/models"}}), \
             patch.object(agent.os.path, "ismount", return_value=True), \
             patch.object(agent, "resolve_user_storage_root", return_value=FakeRoot()), \
             patch.object(agent, "user_encryption_status", return_value={"state": "locked"}):
            with self.assertRaisesRegex(RuntimeError, "fscrypt locked: data:/private"):
                agent.workload_storage_preflight(spec, validator=validator)


if __name__ == "__main__":
    unittest.main()
