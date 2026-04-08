import cube

# ---------------------------------------------------------------------------
# Paths — rooted under CUBE_CACHE_DIR (default ~/.cube/osworld-cube/)
# ---------------------------------------------------------------------------
OSWORLD_BASE_DIR = cube.get_cache_dir("osworld-cube")
OSWORLD_REPO_DIR = OSWORLD_BASE_DIR / "OSWorld"
OSWORLD_VM_DIR = OSWORLD_BASE_DIR / "vm_data"
OSWORLD_CACHE_DIR = OSWORLD_BASE_DIR / "cache"


from osworld_cube.computer import (
    Computer13,
    ComputerBase,
    ComputerConfig,
    PyAutoGUIComputer,
)
from osworld_cube.task import OSWorldTask
from osworld_cube.benchmark import OSWorldBenchmark, OSWorldTaskConfig
from osworld_cube.debug import make_debug_agent, get_debug_benchmark

__all__ = [
    # Tool classes
    "ComputerBase",
    "Computer13",
    "PyAutoGUIComputer",
    # Config classes
    "ComputerConfig",
    # Task / benchmark
    "OSWorldTask",
    "OSWorldBenchmark",
    "OSWorldTaskConfig",
    # Debug helpers
    "make_debug_agent",
    "get_debug_benchmark",
    # Paths
    "OSWORLD_BASE_DIR",
    "OSWORLD_REPO_DIR",
    "OSWORLD_VM_DIR",
    "OSWORLD_CACHE_DIR",
]
