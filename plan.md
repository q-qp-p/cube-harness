# Plan: pre-populate task_metadata before benchmark instantiation

## Goal

`task_metadata` should be available on the benchmark class **before** any instance is created — without calling `setup()`. This allows harnesses, UIs, and tooling to inspect tasks without spinning up infrastructure.

### Mechanism (cube-standard #77)

- `install()` / `uninstall()` are now **classmethods** — callable as `MyBenchmark.install()`.
- `install()` is responsible for downloading data **and** saving `task_metadata.json` next to the benchmark module, containing **all tasks from all splits** (no filtering).
- `__init_subclass__` auto-loads `task_metadata.json` at import time if `task_metadata = {}`.
- `setup()` raises a clear `RuntimeError` if `task_metadata` is still empty.
- Splits/filters stay in user-land via `subset_from_list` / `subset_from_glob`.

---

## Cube status

| Cube | Status | Notes |
| ---- | ------ | ----- |
| `arithmetic-cube` | ✅ nothing to do | Static hardcoded dict at class definition |
| `miniwob` | ✅ done | Replaced intermediate `miniwob_tasks.json` with proper `task_metadata.json` (generated via `TaskMetadata.model_dump()`); `task_metadata = {}` placeholder lets `__init_subclass__` auto-load it |
| `webarena-verified` | ✅ done | `install()` calls `WebArenaVerified().get_tasks()` and saves `task_metadata.json`; idempotent (skips if file exists) |
| `swebench-verified-cube` | ✅ done | `install()` downloads HF `test` split, saves JSON; `_setup()` applies filters from pre-loaded metadata |
| `swebench-live-cube` | ✅ done | `install()` downloads all 4 HF splits (verified/full/test/lite), merges 1895 unique tasks with priority `verified>full>test>lite`, logs per-field diffs on conflict; `named_subsets` on `BenchmarkMetadata` maps split names to `subset_from_glob` patterns; recipe uses `.named_subset("lite")` |
| `osworld-cube` | 🔲 todo | `install()` already downloads dataset; extend it to generate and save JSON from all domain files |
| `terminalbench-cube` | ✅ done | `install()` clones repo, reads task dirs, saves `task_metadata.json` (archive base64-encoded); `_setup()` applies filters + oracle_mode; `Task.reset()` decodes archive (base64 string from JSON, or raw bytes from `_setup()`) |
| `workarena` | 🔲 todo | `install()` calls `get_all_tasks_agents()` for all three levels (l1/l2/l3), merges, saves JSON; `level` becomes a `subset_from_glob` concern |

## Order of work

1. `webarena-verified` — simplest: no download, just library call → JSON ✅
2. `swebench-verified-cube` — single HF split, straightforward ✅
3. `swebench-live-cube` — multi-split merge ✅
4. `terminalbench-cube` — install() already does the heavy lifting
5. `osworld-cube` — install() already downloads; needs JSON generation added
6. `workarena` — multi-level merge, most complex

---

## Phase 2: Lightweight public metadata + per-task execution files

> **Note:** Design decisions here are tentative — to be revisited after all Phase 1 cubes are done.

### Motivation

`task_metadata.json` can become very large (e.g. swebench-live ~557 MB), mostly because every task embeds large fields (`problem_statement`, `patch`, `test_patch`, `fail_to_pass`, etc.) that are only needed at execution time, not for planning/inspection.

### Proposed design

**1. `TaskMetadata` subclasses replace `extra_info` for public fields**

Each cube defines a typed `TaskMetadata` subclass with its lightweight public fields. `extra_info` starts empty and is reserved as the lazy-load destination for heavy execution data:

```python
# swebench_verified_cube/task.py
class SWEBenchVerifiedTaskMetadata(TaskMetadata):
    repo: str
    difficulty: str = "unknown"
    # splits, difficulty, etc. — small, public-facing
    # NO more problem_statement/patch/etc. here
```

`task_metadata.json` then contains only these small fields (serialized with `_type` so `__init_subclass__` reconstructs the right subclass automatically).

**2. Per-task execution files in `~/.cube/cache/<benchmark_name>/tasks/`**

`install()` also writes one JSON file per task:
```
~/.cube/cache/swebench-verified-cube/tasks/django__django-12345.json
  → {"problem_statement": "...", "patch": "...", "test_patch": "...", ...}
```

`uninstall()` removes both `task_metadata.json` and the cache directory.

**3. Base class helper: `Benchmark.load_execution_info(task_id)`**

`cube-standard` adds two classmethods to `Benchmark`:
```python
@classmethod
def execution_cache_dir(cls) -> Path:
    return get_cache_dir(cls.benchmark_metadata.name) / "tasks_execution_info"

@classmethod
def load_execution_info(cls, task_id: str) -> dict[str, Any]:
    cache_file = cls.execution_cache_dir() / f"{task_id}.json"
    if not cache_file.exists():
        raise RuntimeError(f"No execution data for {task_id!r}. Run `{cls.__name__}.install()`.")
    return json.loads(cache_file.read_text())
```

**4. Cube `TaskConfig.make()` loads lazily — transparent to the agent**

```python
def make(self, runtime_context=None, container_backend=None):
    metadata = SWEBenchVerifiedBenchmark.task_metadata[self.task_id]
    exec_info = SWEBenchVerifiedBenchmark.load_execution_info(self.task_id)
    metadata = metadata.model_copy(update={"extra_info": exec_info})
    return SWEBenchVerifiedTask(metadata=metadata, ...)
```

`Task.reset()` and `Task.evaluate()` access `self.metadata.extra_info` exactly as before — **no changes to task logic**.

> **Ray worker constraint:** `TaskConfig.make()` runs in a separate process (Ray worker) where the benchmark is never instantiated and `_setup()` is never called. This means any data injected into `task_metadata` by `_setup()` (e.g. `oracle_mode=True`) is **invisible** to Ray workers — they load `task_metadata.json` fresh. Phase 2 lazy loading via `load_execution_info()` in `TaskConfig.make()` correctly handles this because it reads from disk (not from in-memory ClassVar). This is also why `oracle_mode` and `include_hints` overrides set by the harness don't propagate to Ray workers in the current Phase 1 design.

**5. `named_subsets` glob keys update**

Since `splits`, `difficulty`, etc. are now first-class typed fields (not in `extra_info`), glob keys update from `"extra_info.splits"` → `"splits"`.

### Changes required

| Component | Change |
|-----------|--------|
| `cube-standard` | Add `execution_cache_dir()` + `load_execution_info()` classmethods to `Benchmark` |
| Each cube | Define `XTaskMetadata(TaskMetadata)` with lightweight fields; update `install()` to also write per-task cache files; update `TaskConfig.make()` to call `load_execution_info()`; update `named_subsets` glob keys |
