"""System monitor: CPU, memory, disks, network, battery — the data behind the HUD's System Monitor
panel and the System agent's health checks."""

from __future__ import annotations

import contextlib
import os
import platform
import socket
import sys
import time
from functools import lru_cache
from typing import Any

import psutil

from jarvis.common.clock import iso

GB = 1024**3


@lru_cache(maxsize=1)
def cpu_name() -> str:
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError), open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


@lru_cache(maxsize=1)
def os_name() -> str:
    if sys.platform == "win32":
        release, version = platform.release(), platform.version()
        with contextlib.suppress(Exception):
            build = int(version.split(".")[-1])
            if release == "10" and build >= 22000:
                release = "11"
        return f"Windows {release} ({version})"
    return f"{platform.system()} {platform.release()}"


_VIRTUAL_FS = frozenset({"squashfs", "overlay", "tmpfs", "devtmpfs", "ramfs", "nsfs", "autofs", "proc", "sysfs"})


def _disks() -> list[dict[str, Any]]:
    out = []
    seen: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        if sys.platform == "win32" and ("cdrom" in part.opts or not part.fstype):
            continue
        # Elsewhere: only disks that hold the user's files, not read-only images, RAM disks or network mounts.
        if sys.platform != "win32" and ("ro" in part.opts.split(",") or part.fstype in _VIRTUAL_FS
                                         or part.fstype.startswith("fuse.")):
            continue
        if part.mountpoint in seen or part.mountpoint.startswith(("/snap", "/boot", "/sys", "/proc", "/dev", "/run")):
            continue
        seen.add(part.mountpoint)
        with contextlib.suppress(OSError, PermissionError):
            u = psutil.disk_usage(part.mountpoint)
            out.append({"mount": part.mountpoint, "percent": round(u.percent, 1), "used_gb": round(u.used / GB, 1),
                        "total_gb": round(u.total / GB, 1), "free_gb": round(u.free / GB, 1)})
    if not out:
        with contextlib.suppress(OSError):
            root = os.path.abspath(os.sep)
            u = psutil.disk_usage(root)
            out.append({"mount": root, "percent": round(u.percent, 1), "used_gb": round(u.used / GB, 1),
                        "total_gb": round(u.total / GB, 1), "free_gb": round(u.free / GB, 1)})
    return out


class SystemMonitor:
    def __init__(self) -> None:
        self._last_net: tuple[float, int, int] | None = None
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)

    def sample(self) -> dict[str, Any]:
        """One SystemStats snapshot (blocking for a few ms; call from a worker thread)."""
        now = time.monotonic()
        vm = psutil.virtual_memory()
        net = psutil.net_io_counters()
        up_kbps = down_kbps = 0.0
        if net is not None and self._last_net is not None:
            t0, sent0, recv0 = self._last_net
            dt = max(now - t0, 1e-3)
            up_kbps = max(0.0, (net.bytes_sent - sent0) * 8 / 1000 / dt)
            down_kbps = max(0.0, (net.bytes_recv - recv0) * 8 / 1000 / dt)
        if net is not None:
            self._last_net = (now, net.bytes_sent, net.bytes_recv)
        freq = None
        with contextlib.suppress(Exception):
            f = psutil.cpu_freq()
            freq = round(f.current) if f else None
        battery = None
        with contextlib.suppress(Exception):
            b = psutil.sensors_battery()
            if b is not None:
                battery = {"percent": round(b.percent), "plugged": bool(b.power_plugged)}
        return {
            "ts": iso(),
            "cpu": {"percent": round(psutil.cpu_percent(None), 1),
                    "per_core": [round(x, 1) for x in psutil.cpu_percent(None, percpu=True)],
                    "cores": psutil.cpu_count() or 1, "freq_mhz": freq, "name": cpu_name()},
            "memory": {"percent": round(vm.percent, 1), "used_gb": round((vm.total - vm.available) / GB, 1),
                       "total_gb": round(vm.total / GB, 1)},
            "disks": _disks(),
            "network": {"up_kbps": round(up_kbps, 1), "down_kbps": round(down_kbps, 1),
                        "sent_mb": round((net.bytes_sent if net else 0) / 1_048_576, 1),
                        "recv_mb": round((net.bytes_recv if net else 0) / 1_048_576, 1)},
            "battery": battery,
            "uptime_s": int(time.time() - psutil.boot_time()),
            "processes": len(psutil.pids()),
            "os": os_name(),
            "hostname": socket.gethostname(),
        }


def health_findings(stats: dict[str, Any], top: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """Plain-language findings from a stats snapshot: [{level, area, message}]."""
    out: list[dict[str, str]] = []
    cpu = stats["cpu"]["percent"]
    if cpu >= 90:
        out.append({"level": "warning", "area": "cpu", "message": f"CPU is very busy ({cpu:.0f}%)."})
    mem = stats["memory"]
    if mem["percent"] >= 90:
        out.append({"level": "warning", "area": "memory",
                    "message": f"Memory is almost full ({mem['percent']:.0f}% of {mem['total_gb']} GB)."})
    elif mem["percent"] >= 80:
        out.append({"level": "info", "area": "memory", "message": f"Memory use is high ({mem['percent']:.0f}%)."})
    for d in stats["disks"]:
        if d["percent"] >= 95 or d.get("free_gb", 99) < 5:
            out.append({"level": "warning", "area": "disk",
                        "message": f"Drive {d['mount']} is nearly full ({d.get('free_gb', '?')} GB free)."})
        elif d["percent"] >= 85:
            out.append({"level": "info", "area": "disk",
                        "message": f"Drive {d['mount']} is {d['percent']:.0f}% full."})
    bat = stats.get("battery")
    if bat and not bat["plugged"] and bat["percent"] <= 20:
        out.append({"level": "warning", "area": "battery", "message": f"Battery is low ({bat['percent']}%)."})
    for p in (top or [])[:3]:
        if p.get("cpu", 0) >= 50:
            out.append({"level": "info", "area": "process",
                        "message": f"{p['name']} is using {p['cpu']:.0f}% CPU."})
        if p.get("memory_mb", 0) >= 4000:
            out.append({"level": "info", "area": "process",
                        "message": f"{p['name']} is using {p['memory_mb'] / 1024:.1f} GB of memory."})
    if stats["uptime_s"] > 14 * 86400:
        out.append({"level": "info", "area": "uptime",
                    "message": f"The computer has been running for {stats['uptime_s'] // 86400} days; a restart "
                               "can help."})
    return out
