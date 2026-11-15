#!/usr/bin/env python3
"""Bounded, read-only bridge from the host-agent to GPU inventory diagnostics.

The caller must be authorized by the Control Plane. The local agent Unix
socket remains access-controlled; this is not a generic command runner.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

_BDF = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")
_TOOLS = frozenset(("auto", "all", "lspci", "nvidia-smi", "amd-smi", "rocm-smi"))
_MAX_OUTPUT = 512 * 1024


def gpu_agent_operation(request):
    """Run only the fixed GPU scanner subcommands with validated arguments."""
    if not isinstance(request, dict):
        raise ValueError("invalid GPU request")

    op = request.get("op")
    if op == "gpu_inventory_scan":
        args = ("scan", "--json")
    elif op == "gpu_diagnostics":
        bdf = request.get("pci_bus_id")
        tool = request.get("tool", "auto")
        if not isinstance(bdf, str) or not _BDF.fullmatch(bdf):
            raise ValueError("invalid PCI address")
        if not isinstance(tool, str) or tool not in _TOOLS:
            raise ValueError("unsupported GPU diagnostic tool")
        args = ("diagnose", bdf, "--tool", tool)
    else:
        raise ValueError("unsupported GPU operation")

    scanner = Path(__file__).with_name("gpu_inventory_scan.py")
    if not scanner.is_file():
        raise RuntimeError("GPU inventory scanner is not installed")

    try:
        process = subprocess.run(
            [sys.executable, str(scanner), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GPU diagnostics timed out") from exc
    if process.returncode:
        detail = process.stderr.strip()[:500] or "GPU diagnostic failed"
        raise RuntimeError(detail)
    if len(process.stdout.encode("utf-8")) > _MAX_OUTPUT:
        raise RuntimeError("GPU diagnostic response too large")

    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid GPU scanner output") from exc
    if not isinstance(data, dict):
        raise RuntimeError("invalid GPU scanner response")

    return {"ok": True, "result": data}
