#!/usr/bin/env python3
"""Read-only PCI graphics inventory and allowlisted GPU diagnostics.

Does not bind drivers, alter devices, or write to the platform database.
The host-agent may later call scan_devices() and run_diagnostics() from
an admin-authorized RPC; never expose this module as a general command runner.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SYSFS_PCI = Path('/sys/bus/pci/devices')
BDF_RE = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$')
GRAPHICS_CLASSES = {0x0300, 0x0301, 0x0302, 0x0380}
TOOLS = ('lspci', 'nvidia-smi', 'amd-smi', 'rocm-smi')
MAX_CAPTURE = 128 * 1024


def read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding='utf-8', errors='replace').strip()
        return value or None
    except (OSError, ValueError):
        return None


def read_int(path: Path) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text, 0)
    except ValueError:
        try:
            return int(text, 10)
        except ValueError:
            return None


def read_hex(path: Path) -> str | None:
    value = read_int(path)
    return f'0x{value:04x}' if value is not None else None


def link_basename(path: Path) -> str | None:
    try:
        return path.resolve(strict=True).name if path.is_symlink() else None
    except OSError:
        return None


def run_readonly(command: list[str], *, timeout: int = 8) -> dict[str, Any]:
    """Execute a caller-constructed, allowlisted read-only command without shell."""
    if not command or command[0] not in TOOLS:
        raise ValueError('diagnostic tool is not allowlisted')
    executable = shutil.which(command[0])
    if not executable:
        return {'command': command, 'status': 'unavailable',
                'exit_code': None, 'stdout': '', 'stderr': 'tool not installed'}
    try:
        completed = subprocess.run(
            [executable, *command[1:]], capture_output=True, text=True,
            errors='replace', check=False, timeout=timeout,
        )
        return {
            'command': command,
            'status': 'ok' if completed.returncode == 0 else 'error',
            'exit_code': completed.returncode,
            'stdout': completed.stdout[:MAX_CAPTURE],
            'stderr': completed.stderr[:MAX_CAPTURE],
            'truncated': (len(completed.stdout) > MAX_CAPTURE
                          or len(completed.stderr) > MAX_CAPTURE),
        }
    except subprocess.TimeoutExpired:
        return {'command': command, 'status': 'timeout',
                'exit_code': None, 'stdout': '', 'stderr': 'timeout'}
    except OSError as exc:
        return {'command': command, 'status': 'error',
                'exit_code': None, 'stdout': '', 'stderr': str(exc)}


def read_hwmon(device: Path) -> list[dict[str, Any]]:
    """Return hwmon readings as raw kernel units, without inventing conversions."""
    result = []
    base = device / 'hwmon'
    if not base.is_dir():
        return result
    for hwmon in sorted(base.glob('hwmon*')):
        entries: dict[str, Any] = {}
        for pattern in ('temp*_input', 'temp*_label', 'fan*_input',
                        'power*_input', 'power*_average', 'freq*_input',
                        'in*_input', 'pwm*'):
            for item in sorted(hwmon.glob(pattern))[:32]:
                value = read_text(item)
                if value is not None:
                    entries[item.name] = value
        if entries:
            result.append({'name': read_text(hwmon / 'name'), 'readings': entries})
    return result


def scan_devices(sysfs_root: Path = SYSFS_PCI, *, lookup_names: bool = True) -> list[dict[str, Any]]:
    """Discover *all* PCI graphics adapters, independent of vendor or driver."""
    devices = []
    if not sysfs_root.is_dir():
        return devices
    for device in sorted(sysfs_root.iterdir()):
        bdf = device.name
        if not BDF_RE.fullmatch(bdf):
            continue
        class_code = read_int(device / 'class')
        if class_code is None or (class_code >> 8) not in GRAPHICS_CLASSES:
            continue
        driver = link_basename(device / 'driver')
        pci_name = None
        if lookup_names:
            result = run_readonly(['lspci', '-Dnn', '-s', bdf], timeout=3)
            if result['status'] == 'ok':
                pci_name = result['stdout'].strip() or None
        drm = device / 'drm'
        render_nodes = sorted(p.name for p in drm.glob('renderD*')) if drm.is_dir() else []
        card_nodes = sorted(p.name for p in drm.glob('card[0-9]*')) if drm.is_dir() else []
        devices.append({
            'pci_bus_id': bdf,
            'pci_class': f'0x{class_code:06x}',
            'pci_name': pci_name,
            'vendor_id': read_hex(device / 'vendor'),
            'device_id': read_hex(device / 'device'),
            'subsystem_vendor_id': read_hex(device / 'subsystem_vendor'),
            'subsystem_device_id': read_hex(device / 'subsystem_device'),
            'revision': read_hex(device / 'revision'),
            'driver_in_use': driver,
            'driver_bound': driver is not None,
            'numa_node': read_int(device / 'numa_node'),
            'iommu_group': link_basename(device / 'iommu_group'),
            'pcie': {
                'max_link_speed': read_text(device / 'max_link_speed'),
                'max_link_width': read_int(device / 'max_link_width'),
                'current_link_speed': read_text(device / 'current_link_speed'),
                'current_link_width': read_int(device / 'current_link_width'),
            },
            'amdgpu': {
                'vram_total_bytes': read_int(device / 'mem_info_vram_total'),
                'vram_used_bytes': read_int(device / 'mem_info_vram_used'),
                'vram_visible_bytes': read_int(device / 'mem_info_vis_vram_total'),
                'gpu_busy_percent': read_int(device / 'gpu_busy_percent'),
                'memory_busy_percent': read_int(device / 'mem_busy_percent'),
                'unique_id': read_text(device / 'unique_id'),
            },
            'drm': {'card_nodes': card_nodes, 'render_nodes': render_nodes},
            'hwmon': read_hwmon(device),
        })
    return devices


def reconcile_inventory(detected: list[dict[str, Any]],
                        registered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify detected and DB records for the SAME host; never write to DB.

    A BDF match is only provisional, not proof of the same physical card after
    a hardware swap. The admin must confirm replacement/reassignment.
    """
    db_by_bdf = {r.get('pci_bus_id'): r for r in registered if r.get('pci_bus_id')}
    seen: set[str] = set()
    merged = []
    for gpu in detected:
        bdf = gpu['pci_bus_id']
        record = db_by_bdf.get(bdf)
        row = {'scan_status': 'registered' if record else 'new',
               'detected': gpu, 'registered': record}
        merged.append(row)
        if record:
            seen.add(bdf)
    for record in registered:
        if record.get('pci_bus_id') not in seen:
            merged.append({'scan_status': 'missing',
                           'detected': None, 'registered': record})
    return merged


def run_diagnostics(bdf: str, tool: str = 'auto',
                    sysfs_root: Path = SYSFS_PCI) -> dict[str, Any]:
    if not BDF_RE.fullmatch(bdf):
        raise ValueError('invalid PCI BDF')
    found = {d['pci_bus_id']: d for d in scan_devices(sysfs_root, lookup_names=False)}
    if bdf not in found:
        raise ValueError('requested PCI BDF is not a detected graphics adapter')
    device = found[bdf]
    vendor = device['vendor_id']
    requested = ['lspci']
    if tool == 'auto':
        if vendor == '0x10de':
            requested.append('nvidia-smi')
        elif vendor == '0x1002':
            requested.extend(['amd-smi', 'rocm-smi'])
    elif tool == 'all':
        requested = list(TOOLS)
    elif tool in TOOLS:
        requested = [tool]
    else:
        raise ValueError('unknown diagnostic tool')
    output = []
    for name in requested:
        if name == 'lspci':
            commands = [['lspci', '-Dvvk', '-s', bdf]]
            scope = 'selected_gpu'
        elif name == 'nvidia-smi':
            commands = [['nvidia-smi', '-q', '-i', bdf]]
            scope = 'selected_gpu'
        elif name == 'amd-smi':
            commands = [['amd-smi', 'static', '-g', bdf],
                        ['amd-smi', 'metric', '-g', bdf]]
            scope = 'selected_gpu'
        else:
            # rocm-smi indexes are not PCI BDFs. Avoid passing an incorrect
            # index; its read-only report can contain multiple cards.
            commands = [['rocm-smi', '--showallinfo']]
            scope = 'all_supported_gpus'
        for command in commands:
            output.append({'tool': name, 'scope': scope,
                           **run_readonly(command)})
    return {'pci_bus_id': bdf, 'results': output}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read-only host GPU inventory scanner')
    sub = parser.add_subparsers(dest='command', required=True)
    p_scan = sub.add_parser('scan', help='scan all PCI graphics devices')
    p_scan.add_argument('--json', action='store_true', help='emit pretty JSON')
    p_diag = sub.add_parser('diagnose', help='run allowlisted read-only diagnostics')
    p_diag.add_argument('bdf', help='PCI address, e.g. 0000:09:00.0')
    p_diag.add_argument('--tool', choices=('auto', 'all', *TOOLS), default='auto')
    args = parser.parse_args(argv)
    try:
        if args.command == 'scan':
            devices = scan_devices()
            result = {
                'schema_version': 1,
                'host': socket.gethostname(),
                'scanned_at': datetime.now(timezone.utc).isoformat(),
                'devices': devices,
                'tools_available': {tool: shutil.which(tool) is not None for tool in TOOLS},
            }
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f'Found {len(devices)} PCI graphics adapter(s)')
                for device in devices:
                    print(f"{device['pci_bus_id']}: {device['pci_name'] or device['vendor_id']}"
                          f" (driver: {device['driver_in_use'] or 'none'})")
        else:
            print(json.dumps(run_diagnostics(args.bdf, args.tool), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(f'gpu scanner: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
