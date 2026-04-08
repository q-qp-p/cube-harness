"""
OSWorldBenchmark and OSWorldTaskConfig — CUBE benchmark for the OSWorld desktop-automation suite.

Entry point:
    bench = OSWorldBenchmark(default_tool_config=ComputerConfig())
    bench.setup()
    for task_config in bench.get_task_configs():
        task = task_config.make()
        obs, info = task.reset()
        ...
        task.close()

Filter by domain or other metadata field after setup():
    chrome_bench = bench.subset_from_glob("extra_info.domain", "chrome")
"""

from __future__ import annotations

import enum
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Generator
from copy import deepcopy
from dotenv import load_dotenv
from pathlib import Path
from typing import ClassVar

from cube.benchmark import Benchmark, BenchmarkMetadata
from cube.container import ContainerBackend
from cube.task import TaskConfig, TaskMetadata
from cube.vm import VMBackend

from osworld_cube.computer import ComputerConfig, _CUBE_CACHE_ROOT
from osworld_cube.task import OSWorldTask

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths — rooted under CUBE_CACHE_DIR (default ~/.cube)
# ---------------------------------------------------------------------------

OSWORLD_BASE_DIR = _CUBE_CACHE_ROOT  # same as cube.get_cache_dir("osworld-cube")
OSWORLD_REPO_DIR = OSWORLD_BASE_DIR / "OSWorld"
OSWORLD_VM_DIR = OSWORLD_BASE_DIR / "vm_data"
OSWORLD_CACHE_DIR = OSWORLD_BASE_DIR / "cache"

# Pinned OSWorld commit for reproducibility
OSWORLD_COMMIT = "e695a10"

_TASK_METADATA_JSON = Path(__file__).parent / "task_metadata.json"


# ---------------------------------------------------------------------------
# .env helper
# ---------------------------------------------------------------------------


def ensure_proxy_config_in_env(env_path: Path = Path(".env")) -> None:
    """Append PROXY_CONFIG_FILE to .env if it is not already defined there.

    The value mirrors the default set in computer.py so that desktop_env
    resolves the path correctly regardless of the current working directory.
    """
    key = "PROXY_CONFIG_FILE"
    value = str(OSWORLD_REPO_DIR / "evaluation_examples" / "settings" / "proxy" / "dataimpulse.json")

    if env_path.exists():
        for line in env_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                logger.debug(f"{key} already present in {env_path}, skipping.")
                return

    with env_path.open("a") as f:
        f.write(f"\n{key}={value}\n")
    logger.info(f"Appended {key} to {env_path}")


# ---------------------------------------------------------------------------
# OSWorldTestSet
# ---------------------------------------------------------------------------


class OSWorldTestSet(str, enum.Enum):
    """Valid test-set index files shipped with the OSWorld repo."""

    TEST_ALL = "test_all.json"
    TEST_INFEASIBLE = "test_infeasible.json"
    TEST_NOGDRIVE = "test_nogdrive.json"
    TEST_SMALL = "test_small.json"


# ---------------------------------------------------------------------------
# OSWorldTaskConfig
# ---------------------------------------------------------------------------


class OSWorldTaskConfig(TaskConfig):
    """
    Serialisable config for a single OSWorld task.

    Fields:
        task_id:     inherited from TaskConfig
        tool_config: inherited from TaskConfig
        seed:        inherited (ignored for OSWorld — tasks are deterministic)
        vm_backend:  VMBackend to use for this task (passed by benchmark.get_task_configs()).

    make() looks up TaskMetadata from OSWorldBenchmark.task_metadata (a ClassVar
    populated by OSWorldBenchmark.setup()).
    """

    vm_backend: VMBackend | None = None
    use_som: bool = False

    def make(
        self,
        runtime_context: dict | None = None,
        container_backend: ContainerBackend | None = None,
    ) -> OSWorldTask:
        """Instantiate OSWorldTask from this config."""
        metadata = OSWorldBenchmark.task_metadata[self.task_id]

        if self.tool_config is None:
            raise ValueError(
                f"OSWorldTaskConfig for task '{self.task_id}' has no tool_config. "
                "Pass default_tool_config=ComputerConfig(...) to OSWorldBenchmark."
            )
        return OSWorldTask(
            metadata=metadata,
            tool_config=self.tool_config,
            vm_backend=self.vm_backend,
            runtime_context=runtime_context,
            container_backend=container_backend,
            use_som=self.use_som,
        )


# ---------------------------------------------------------------------------
# OSWorldBenchmark
# ---------------------------------------------------------------------------


class OSWorldBenchmark(Benchmark):
    """
    CUBE benchmark wrapping the OSWorld desktop-automation evaluation suite.

    Reference: https://github.com/xlang-ai/OSWorld

    Class-level attributes (required by cube.benchmark.Benchmark):
        benchmark_metadata:  ClassVar[BenchmarkMetadata]
        task_metadata:       ClassVar[dict[str, TaskMetadata]]  (placeholder {}; populated in _setup())
        task_config_class:   type[TaskConfig] = OSWorldTaskConfig

    Constructor params (set by benchmark users):
        default_tool_config:  ComputerConfig  — how to connect to the VM (action_space selects variant)
        use_som:              bool            — Set-of-Marks mode for all tasks
        vm_backend:           VMBackend | None — backend to provision VMs; None = attach externally

    To filter by domain or any other metadata field, call subset_from_glob() after setup():
        bench.setup()
        chrome_bench = bench.subset_from_glob("extra_info.domain", "chrome")
    """

    # ------------------------------------------------------------------
    # Required class variables
    # ------------------------------------------------------------------

    benchmark_metadata: ClassVar[BenchmarkMetadata] = BenchmarkMetadata(
        name="osworld",
        version="1.0.0",
        description=("OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments"),
        authors=["Tianbao Xie et al."],
        license="CC-BY-4.0",
        requirements={
            "vm": "Ubuntu 22.04 (docker or vmware)",
            "ram_gb": 8,
            "disk_gb": 40,
        },
        num_tasks=369,
        tags=["desktop", "gui", "multimodal"],
        named_subsets={
            "test_all": ("extra_info.test_sets", "*'test_all'*"),
            "test_small": ("extra_info.test_sets", "*'test_small'*"),
            "test_nogdrive": ("extra_info.test_sets", "*'test_nogdrive'*"),
            "test_infeasible": ("extra_info.test_sets", "*'test_infeasible'*"),
        },
    )
    # task_metadata: populated automatically at import time in Benchmark.__init_subclass__
    task_config_class: ClassVar[type[TaskConfig]] = OSWorldTaskConfig

    # ------------------------------------------------------------------
    # Instance fields
    # ------------------------------------------------------------------
    default_tool_config: ComputerConfig = ComputerConfig()

    use_som: bool = False
    """Enable Set-of-Marks annotation for all tasks in this benchmark run."""

    vm_backend: VMBackend | None = None
    """VM backend used to provision VMs for each task. If None, tasks will fail
    unless a VM is attached externally via computer.attach_vm()."""

    # ------------------------------------------------------------------
    # get_task_configs() override — inject vm_backend into each config
    # ------------------------------------------------------------------

    def get_task_configs(self) -> Generator[TaskConfig, None, None]:
        """Yield task config objects, injecting vm_backend and use_som from the benchmark."""
        tc_cls = self.task_config_class
        assert issubclass(tc_cls, OSWorldTaskConfig)
        for tm in self.task_metadata.values():
            yield tc_cls(
                task_id=tm.id,
                tool_config=self.default_tool_config,
                seed=None,
                vm_backend=self.vm_backend,
                use_som=self.use_som,
            )

    # ------------------------------------------------------------------
    # _setup()
    # ------------------------------------------------------------------

    def _setup(self) -> None:
        """Rresolve machine-specific paths in task_metadata.

        task_metadata.json is bundled with the package and must remain portable
        (machine-agnostic), so settings_file paths inside config items are stored
        as relative paths (relative to the OSWorld repo root).  OSWORLD_REPO_DIR,
        however, is machine-specific (derived from _CUBE_CACHE_ROOT / an env var).
        _fix_config_paths() therefore runs here at setup() time — not in install() —
        so that absolute paths are resolved against the correct local OSWORLD_REPO_DIR
        on whatever machine is running the benchmark.
        """
        provider = type(self.vm_backend).__name__ if self.vm_backend else "none"
        logger.info(f"Setting up OSWorldBenchmark (provider={provider})...")

        # Only skip loading if this instance already has its own shadow (i.e. was
        # already set up).  We deliberately do NOT guard on the class-level attr
        # because that would prevent a fresh instance from loading its own task
        # set when a previous setup already populated the ClassVar with a different set.
        if "task_metadata" not in self.__dict__:
            # Fix absolute settings_file paths in config items (repo-relative → absolute)
            loaded = {}
            for tid, tm in type(self).task_metadata.items():
                fixed_config = self._fix_config_paths(tm.extra_info.get("config", []))
                extra = {**tm.extra_info, "config": fixed_config}
                loaded[tid] = tm.model_copy(update={"extra_info": extra})

            # Populate instance-level shadow for test isolation (each Benchmark
            # instance sees its own view, e.g. after subset_from_glob).
            object.__setattr__(self, "task_metadata", loaded)
            # Also update the class-level attr so make() can find tasks via the
            # ClassVar in the same process without needing to re-run setup().
            type(self).task_metadata = loaded

        # OSWorld manages its own VM lifecycle via desktop_env — no shared runtime
        # infrastructure is needed. Populate _runtime_context to suppress the
        # Benchmark.setup() warning that fires when it is left empty.
        self._runtime_context = {"osworld": True}

        logger.info(f"OSWorldBenchmark ready with {len(self.task_metadata)} tasks")

    def close(self) -> None:
        """
        Clean up benchmark resources.

        VM teardown is handled per-task by Computer.close() / OSWorldTask.close().
        No global VM resources to release here.
        """
        logger.info("Closing OSWorldBenchmark — no global resources to release")

    @staticmethod
    def _fix_config_paths(config: list) -> list:
        """Rewrite relative settings_file paths in config items to absolute paths."""
        result = deepcopy(config)
        for item in result:
            params = item.get("parameters", {})
            if "settings_file" in params:
                params["settings_file"] = str(OSWORLD_REPO_DIR / params["settings_file"])
        return result

    @staticmethod
    def _clone_osworld_repo() -> None:
        """Clone and pin the OSWorld repository to OSWORLD_COMMIT."""
        OSWORLD_BASE_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "https://github.com/xlang-ai/OSWorld", str(OSWORLD_REPO_DIR)],
            check=True,
        )
        subprocess.run(
            ["git", "checkout", OSWORLD_COMMIT],
            cwd=str(OSWORLD_REPO_DIR),
            check=True,
        )
        OSWORLD_VM_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # install() / uninstall()
    # ------------------------------------------------------------------

    @classmethod
    def install(cls) -> None:
        """Clone OSWorld repo and save task_metadata.json (tasks from all test sets).

        The repo is always cloned if missing — it is needed at task execution time
        for settings_file paths in task config items. task_metadata.json is only
        generated if it does not already exist.
        """
        logger.info("Installing OSWorld benchmark...")
        # Repo is needed at task execution time; always ensure it is present.
        if not OSWORLD_REPO_DIR.exists():
            OSWorldBenchmark._clone_osworld_repo()
            logger.info(f"OSWorld repo cloned to {OSWORLD_REPO_DIR}")
        else:
            logger.info(f"OSWorld repo already present at {OSWORLD_REPO_DIR}")
        ensure_proxy_config_in_env()
        load_dotenv()  # Load the .env file
        logger.info(f"Set PROXY_CONFIG_FILE={os.environ.get('PROXY_CONFIG_FILE', 'not set')}")
        logger.info("VM images will be downloaded automatically on first task reset.")

        if _TASK_METADATA_JSON.exists():
            logger.info("task_metadata.json already exists, skipping metadata generation")
            return

        eval_examples_dir = OSWORLD_REPO_DIR / "evaluation_examples"

        # Collect which test sets each task_id belongs to
        task_sets: dict[str, list[str]] = {}
        task_raw: dict[str, dict] = {}

        for test_set in OSWorldTestSet:
            test_set_file = eval_examples_dir / test_set.value
            if not test_set_file.exists():
                logger.warning(f"Test set file not found: {test_set_file}")
                continue
            with open(test_set_file) as f:
                tasks_by_domain: dict[str, list[str]] = json.load(f)

            set_name = test_set.value.replace(".json", "")
            for domain_name, task_ids in tasks_by_domain.items():
                for task_id in task_ids:
                    task_sets.setdefault(task_id, []).append(set_name)
                    if task_id not in task_raw:
                        task_file = eval_examples_dir / "examples" / domain_name / f"{task_id}.json"
                        if not task_file.exists():
                            logger.warning(f"Task file not found: {task_file}")
                            continue
                        try:
                            with open(task_file) as f:
                                td = json.load(f)
                            task_raw[task_id] = {"domain": domain_name, "data": td}
                        except Exception as e:
                            logger.error(f"Failed to load task {task_id}: {e}")

        metadata: dict[str, TaskMetadata] = {}
        for task_id, info in task_raw.items():
            td = info["data"]
            domain_name = info["domain"]
            tm = TaskMetadata(
                id=td.get("id", task_id),
                abstract_description=td.get("desc", td.get("instruction", "")),
                extra_info={
                    "instruction": td.get("instruction", ""),
                    "domain": domain_name,
                    "test_sets": task_sets.get(task_id, []),
                    "snapshot": td.get("snapshot", "init_state"),
                    "config": td.get("config", []),
                    "evaluator": td.get("evaluator", {}),
                    "related_apps": td.get("related_apps", []),
                },
            )
            metadata[tm.id] = tm

        _TASK_METADATA_JSON.write_text(json.dumps([tm.model_dump() for tm in metadata.values()], indent=2))
        cls.task_metadata = metadata
        logger.info(f"Saved {len(metadata)} tasks to {_TASK_METADATA_JSON}")

    @classmethod
    def uninstall(cls) -> None:
        """Remove task_metadata.json and the cloned OSWorld repo."""
        if _TASK_METADATA_JSON.exists():
            _TASK_METADATA_JSON.unlink()
            cls.task_metadata = {}
            logger.info(f"Removed {_TASK_METADATA_JSON}")
        if OSWORLD_REPO_DIR.exists():
            shutil.rmtree(OSWORLD_REPO_DIR)
            logger.info(f"Removed OSWorld repo at {OSWORLD_REPO_DIR}")
