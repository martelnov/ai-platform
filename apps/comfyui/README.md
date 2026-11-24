# ComfyUI for AI Platform (phase 1: CPU image)

This is a **temporary CPU smoke-test image**, not yet the user-facing app.
It does not modify Control Plane, Gateway, host-agent or PostgreSQL.

Build on `ailab` (requires internet and sufficient Docker disk space):

```bash
sudo docker build -t ai-platform-comfyui:cpu-test -f /AI/apps/ai-platform/apps/comfyui/Dockerfile /AI/apps/ai-platform/apps/comfyui
```

Local-only smoke run, with **no host data mounted**:

```bash
sudo docker run -d --rm --name ai-comfyui-smoke -p 127.0.0.1:8188:8188 ai-platform-comfyui:cpu-test
curl -I http://127.0.0.1:8188/
sudo docker logs --tail=50 ai-comfyui-smoke
sudo docker stop ai-comfyui-smoke
```

The future user-facing workload runtime will start independent containers
from one shared image, with `--user <uid>:<gid>`, their private `/workspace`
and `/models/user`, and `/AI/models/common` mounted read-only as
`/models/common`. It must bind at most 1 or 2 exclusive NVIDIA GPU UUIDs,
which are granted only under the user's reservation and runtime allocation.
Do **not** publish ComfyUI directly to the public internet or expose it
between users: integrate access with AI Platform's authenticated connector.

Nightly deduplication and forbidding fscrypt on user models belong to a
separate platform-level implementation, not to ComfyUI itself.

The Dockerfile currently checks out ComfyUI's default branch and records the
exact commit in `/opt/comfyui.commit`; pin the revision when switching to a
production CUDA image.
