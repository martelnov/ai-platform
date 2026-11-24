# AI Platform: temporary CPU-only ComfyUI image for integration testing.
# GPU images will be built separately; this does not install NVIDIA drivers.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git ffmpeg libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Record the upstream commit in the image. Pin it before production roll-out.
RUN git clone --depth 1 https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI \
 && git -C /opt/ComfyUI rev-parse HEAD > /opt/comfyui.commit

# PyTorch CPU build: the host currently has no NVIDIA GPU/driver.
RUN python -m pip install --no-cache-dir \
      torch torchvision torchaudio \
      --index-url https://download.pytorch.org/whl/cpu \
 && python -m pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt

COPY extra_model_paths.yaml /opt/ComfyUI/extra_model_paths.yaml

# In production Docker --user will override this with the owning platform UID:GID.
RUN groupadd --gid 10001 comfy \
 && useradd --uid 10001 --gid 10001 --create-home \
      --shell /usr/sbin/nologin comfy \
 && mkdir -p /workspace/user /workspace/input /workspace/output /workspace/temp \
      /models/common /models/user \
 && chown -R 10001:10001 /workspace /models/user

USER 10001:10001
WORKDIR /opt/ComfyUI
EXPOSE 8188

CMD ["python", "main.py", "--cpu", "--listen", "0.0.0.0", "--port", "8188", \
     "--models-directory", "/models/user", \
     "--user-directory", "/workspace/user", \
     "--input-directory", "/workspace/input", \
     "--output-directory", "/workspace/output", \
     "--temp-directory", "/workspace/temp"]
