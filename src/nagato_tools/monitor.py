"""System monitoring tool for NagatoFSM.

Provides nagato_is_agent_running() to check if an agent is likely running
based on CPU, GPU, and VRAM utilization thresholds.
"""

import atexit
import json
from pathlib import Path
from typing import Any, Optional

# Initialize psutil with graceful degradation
HAS_PSUTIL = False
psutil = None
try:
    import psutil
    # Initialize CPU baseline at import time (fixes first-call 0.0)
    psutil.cpu_percent(interval=None, percpu=True)
    HAS_PSUTIL = True
except (ImportError, Exception):
    HAS_PSUTIL = False

# Initialize nvidia-ml-py (pynvml) with graceful degradation
HAS_NVIDIA = False
pynvml = None
try:
    import nvidia_ml_py as pynvml
    pynvml.nvmlInit()
    HAS_NVIDIA = True
except (ImportError, NameError):
    HAS_NVIDIA = False
except Exception:
    # Catch NVMLError and any other exceptions from nvmlInit
    HAS_NVIDIA = False

if HAS_NVIDIA and pynvml is not None:
    atexit.register(pynvml.nvmlShutdown)


def _get_workspace_root(ctx: Optional[Any] = None) -> Path:
    """Get workspace root from context or fall back to current working directory."""
    if ctx is not None and hasattr(ctx, 'workspace_root'):
        return ctx.workspace_root
    return Path.cwd()


def _get_config(ctx: Optional[Any] = None):
    """Read monitor thresholds from config.yaml."""
    import yaml
    
    workspace_root = _get_workspace_root(ctx)
    config_path = workspace_root / ".nagato" / "config.yaml"
    defaults = {
        "gpu_compute_threshold": 90,
        "cpu_core_threshold": 30,
        "vram_threshold_pct": 95,
    }
    
    if not config_path.exists():
        return defaults
    
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        monitor_config = config.get("fsm", {}).get("monitor", {})  # host-side config key
        return {
            "gpu_compute_threshold": monitor_config.get("gpu_compute_threshold", defaults["gpu_compute_threshold"]),
            "cpu_core_threshold": monitor_config.get("cpu_core_threshold", defaults["cpu_core_threshold"]),
            "vram_threshold_pct": monitor_config.get("vram_threshold_pct", defaults["vram_threshold_pct"]),
        }
    except Exception:
        return defaults


def _get_top_consumer():
    """Get the process with highest CPU usage."""
    if not HAS_PSUTIL or psutil is None:
        return None
    try:
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent']):
            try:
                info = proc.info
                if info['cpu_percent'] is not None and info['cpu_percent'] > 0:
                    processes.append((info['cpu_percent'], info['pid'], info['name']))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        if processes:
            processes.sort(reverse=True)
            top = processes[0]
            return f"{top[2]} (PID {top[1]}) - {top[0]:.1f}% CPU"
    except Exception:
        pass
    return None


def nagato_is_agent_running(_ctx: Optional[Any] = None) -> str:
    """
    Check if an agent is likely running based on system resource utilization.
    
    Returns JSON with status and metrics:
    - status: "AGENT_RUN_POSSIBLE" or "AGENT_RUN_UNLIKELY"
    - reason: "GPU_COMPUTE_HIGH" | "CPU_CORE_HIGH" | "VRAM_FULL" | "OK"
    - cpu_max_core_pct: Maximum per-core CPU utilization
    - gpu_compute_pct: GPU compute utilization (0 if no NVIDIA GPU)
    - vram_used_mb: VRAM used in MB (0 if no NVIDIA GPU)
    - vram_total_mb: Total VRAM in MB (0 if no NVIDIA GPU)
    - vram_pct: VRAM utilization percentage (0 if no NVIDIA GPU)
    - top_consumer: Process consuming most CPU (only when threshold exceeded)
    """
    config = _get_config(_ctx)
    gpu_threshold = config["gpu_compute_threshold"]
    cpu_threshold = config["cpu_core_threshold"]
    vram_threshold = config["vram_threshold_pct"]
    
    # CPU: non-blocking, per-core
    cpu_per_core = psutil.cpu_percent(interval=None, percpu=True) if (HAS_PSUTIL and psutil is not None) else []
    cpu_max_core = max(cpu_per_core) if cpu_per_core else 0.0
    
    # GPU and VRAM via pynvml
    gpu_compute_pct = 0.0
    vram_used_mb = 0
    vram_total_mb = 0
    vram_pct = 0.0
    
    if HAS_NVIDIA:
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            if device_count > 0:
                # Use first GPU (index 0)
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                
                # GPU compute utilization
                utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
                gpu_compute_pct = float(utilization.gpu)
                
                # VRAM info
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                vram_used_mb = mem_info.used // (1024 * 1024)
                vram_total_mb = mem_info.total // (1024 * 1024)
                vram_pct = (mem_info.used / mem_info.total) * 100 if mem_info.total > 0 else 0.0
        except Exception:
            pass
    
    # Determine status and reason
    status = "AGENT_RUN_POSSIBLE"
    reason = "OK"
    top_consumer = None
    
    # Check GPU compute high (standalone threshold)
    if gpu_compute_pct > gpu_threshold:
        status = "AGENT_RUN_UNLIKELY"
        reason = "GPU_COMPUTE_HIGH"
        top_consumer = _get_top_consumer()
    # Check split CPU/GPU scenario (both elevated)
    elif cpu_max_core > cpu_threshold and gpu_compute_pct > 30:
        status = "AGENT_RUN_UNLIKELY"
        reason = "CPU_GPU_SPLIT_HIGH"
        top_consumer = _get_top_consumer()
    # Check VRAM full
    elif vram_pct > vram_threshold:
        status = "AGENT_RUN_UNLIKELY"
        reason = "VRAM_FULL"
        top_consumer = _get_top_consumer()
    
    result = {
        "status": status,
        "reason": reason,
        "cpu_max_core_pct": round(cpu_max_core, 1),
        "gpu_compute_pct": round(gpu_compute_pct, 1),
        "vram_used_mb": vram_used_mb,
        "vram_total_mb": vram_total_mb,
        "vram_pct": round(vram_pct, 1),
    }
    
    if top_consumer:
        result["top_consumer"] = top_consumer
    
    return json.dumps(result)


# Non-prefixed aliases for MCP server compatibility
is_agent_running = nagato_is_agent_running
__all__ = ['nagato_is_agent_running']
