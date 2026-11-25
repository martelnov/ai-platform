#!/bin/sh
set -eu

cd /opt/ComfyUI

case "${COMFYUI_MODE:-cpu}" in
    cpu)
        echo "ComfyUI: CPU mode"
        set -- --cpu
        ;;
    gpu)
        echo "ComfyUI: GPU mode"
        python -c 'import torch,sys; ok=torch.cuda.is_available(); print("CUDA available:",ok,flush=True); sys.exit(0 if ok else 1)'
        set --
        ;;
    *)
        echo "Invalid COMFYUI_MODE" >&2
        exit 2
        ;;
esac

exec python main.py "$@" \
    --listen 127.0.0.1 \
    --port 8188 \
    --models-directory /models/user \
    --user-directory /workspace/user \
    --input-directory /workspace/input \
    --output-directory /workspace/output \
    --temp-directory /workspace/temp
