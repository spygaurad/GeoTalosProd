"""
External resource sampler — observes processes from the outside, adds NO tracking
code to the app or to palm_api.

Sources (all subprocess, no Python packages required):
  - GPU   : `nvidia-smi` -> per-device memory.used (MiB) + utilization (%) + name
  - Docker: `docker stats` -> per-container CPU% + memory (bytes)

Sampling runs on a daemon thread and tracks the running maximum of each metric
between start() and stop().
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any


def _visible_gpu_indices() -> set[int] | None:
    """Restrict sampling to CUDA_VISIBLE_DEVICES (so we don't read another
    process's GPU on a shared node). None means 'all'."""
    val = os.environ.get("CUDA_VISIBLE_DEVICES")
    if not val:
        return None
    out = set()
    for tok in val.split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
    return out or None


def _nvidia_smi() -> list[dict[str, Any]]:
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except Exception:
        return []
    allowed = _visible_gpu_indices()
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        idx, name, mem_used, util = parts[:4]
        if allowed is not None and int(idx) not in allowed:
            continue
        gpus.append({"index": int(idx), "name": name,
                     "mem_used_mib": float(mem_used), "util_pct": float(util)})
    return gpus


def _docker_stats(containers: list[str]) -> dict[str, dict[str, float]]:
    if not containers:
        return {}
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}", *containers],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except Exception:
        return {}
    stats: dict[str, dict[str, float]] = {}
    for line in out.strip().splitlines():
        try:
            name, cpu, mem = line.split("\t")
            cpu_pct = float(cpu.strip().rstrip("%"))
            mem_used = _parse_bytes(mem.split("/")[0].strip())
            stats[name] = {"cpu_pct": cpu_pct, "mem_bytes": mem_used}
        except Exception:
            continue
    return stats


def _parse_bytes(s: str) -> float:
    units = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3,
             "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TIB": 1024**4}
    s = s.strip().upper()
    for u in sorted(units, key=len, reverse=True):
        if s.endswith(u):
            return float(s[: -len(u)].strip()) * units[u]
    try:
        return float(s)
    except ValueError:
        return 0.0


class ResourceSampler:
    def __init__(self, containers: list[str] | None = None,
                 sample_gpu: bool = True, sample_self: bool = False,
                 interval: float = 0.5):
        self.containers = containers or []
        self.sample_gpu = sample_gpu
        self.sample_self = sample_self   # this process (for the naive HPC/Colab script)
        self.interval = interval
        self._peak_self_cpu = 0.0
        self._proc = None
        if sample_self:
            try:
                import psutil
                self._proc = psutil.Process()
                self._proc.cpu_percent(None)  # prime
            except Exception:
                self._proc = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # running maxima
        self._peak_gpu_mem_mib = 0.0
        self._peak_gpu_util = 0.0
        self._gpu_names: dict[int, str] = {}
        self._peak_cpu: dict[str, float] = {}
        self._peak_mem: dict[str, float] = {}
        self._peak_total_cpu = 0.0
        self._peak_total_mem = 0.0
        self._samples = 0

    def _tick(self) -> None:
        if self.sample_gpu:
            for g in _nvidia_smi():
                self._gpu_names[g["index"]] = g["name"]
                self._peak_gpu_mem_mib = max(self._peak_gpu_mem_mib, g["mem_used_mib"])
                self._peak_gpu_util = max(self._peak_gpu_util, g["util_pct"])
        stats = _docker_stats(self.containers)
        tick_cpu = 0.0
        tick_mem = 0.0
        for name, s in stats.items():
            self._peak_cpu[name] = max(self._peak_cpu.get(name, 0.0), s["cpu_pct"])
            self._peak_mem[name] = max(self._peak_mem.get(name, 0.0), s["mem_bytes"])
            tick_cpu += s["cpu_pct"]
            tick_mem += s["mem_bytes"]
        # peak of the SUM across containers at one instant (true platform total)
        self._peak_total_cpu = max(self._peak_total_cpu, tick_cpu)
        self._peak_total_mem = max(self._peak_total_mem, tick_mem)
        if self._proc is not None:
            try:
                self._peak_self_cpu = max(self._peak_self_cpu, self._proc.cpu_percent(None))
            except Exception:
                pass
        self._samples += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._tick()
            # docker stats already costs ~1s; pace the rest
            self._stop.wait(self.interval)

    def start(self) -> "ResourceSampler":
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return self.result()

    def result(self) -> dict[str, Any]:
        # Peak RSS of THIS process (authoritative, no sampling needed) for the naive script.
        peak_self_rss_mib = None
        if self.sample_self:
            import resource
            peak_self_rss_mib = round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)  # KB -> MiB
        return {
            "samples": self._samples,
            "gpu_names": sorted(set(self._gpu_names.values())),
            "peak_gpu_mem_mib": round(self._peak_gpu_mem_mib, 1),
            "peak_gpu_util_pct": round(self._peak_gpu_util, 1),
            "peak_self_cpu_pct": round(self._peak_self_cpu, 1) if self.sample_self else None,
            "peak_self_rss_mib": peak_self_rss_mib,
            "peak_total_cpu_pct": round(self._peak_total_cpu, 1),
            "peak_total_mem_mib": round(self._peak_total_mem / 1024**2, 1),
            "containers": {
                name: {
                    "peak_cpu_pct": round(self._peak_cpu.get(name, 0.0), 1),
                    "peak_mem_mib": round(self._peak_mem.get(name, 0.0) / 1024**2, 1),
                }
                for name in self.containers
            },
        }
