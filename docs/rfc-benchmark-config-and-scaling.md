# RFC: BenchmarkConfig, BenchmarkPool, and CompositeBenchmark

**Status**: Proposal — no code yet  
**Replaces**: `docs/BENCHMARK_POOL_DESIGN.md` (PR #283)

---

## Problem

Three separate but related problems that share a root cause:

1. **`subset_from_list` is fragile.** It uses `copy.deepcopy(self)`, which crashes when a benchmark holds OS-level private attrs (subprocess handles, file descriptors, thread locks) created during `setup()`. Workarounds (`__deepcopy__` overrides) have already appeared in the codebase.

2. **Benchmarks can't be serialized or composed.** There is no way to save a configured benchmark suite to JSON and reload it. A `CompositeBenchmark` that combines WorkArena + MiniWob + OSWorld can't be persisted without custom code.

3. **Scaling to multiple server instances requires ugly workarounds.** WorkArena and WebArena degrade under load (~7 concurrent agents per server). To distribute load across N servers today, you have to manually manage N benchmark objects and write your own dispatch logic. `BenchmarkPool` should do this, but it needs a way to instantiate N benchmarks from one config.

All three problems have the same root cause: **`Benchmark` mixes config (serializable data) with runtime state (OS handles, connections)**.

---

## Proposed Solution

Split `Benchmark` into two layers, mirroring the existing `TaskConfig` / `Task` pattern that already works well in the codebase:

```
BenchmarkConfig   →  make(infra_config)  →  Benchmark
  (pure data)                               (runtime state)
  serializable                              not serializable
  subset_from_list()                        _setup() / close()
  composable                                get_task_configs()
```

Then `BenchmarkPool` and `CompositeBenchmark` become natural compositions of `BenchmarkConfig`.

---

## Part 1 — BenchmarkConfig

### The core abstraction

`BenchmarkConfig` is the serializable description of a benchmark: what tasks exist, how they're parameterized, what resources they need. It holds no runtime state.

```python
class BenchmarkConfig(TypedBaseModel, ABC):
    benchmark_metadata: ClassVar[BenchmarkMetadata]   # same as today
    task_metadata: ClassVar[dict[str, TaskMetadata]]   # populated at import time
    task_config_class: ClassVar[type[TaskConfig]]

    resources: list[ResourceConfig] = Field(default_factory=list)
    default_tool_config: ToolConfig | None = None
    seed_generator: AbstractSeedGenerator | None = None

    def subset_from_list(self, tasks: list[str]) -> "BenchmarkConfig":
        """Trivial dict filter — no deepcopy, no workarounds."""
        new = self.model_copy()
        object.__setattr__(new, "task_metadata", {tid: tm for tid, tm in self.task_metadata.items() if tid in set(tasks)})
        return new

    @abstractmethod
    def make(self, infra_config: InfraConfig | None = None) -> "Benchmark":
        """Provision resources idempotently, then return a live Benchmark."""
        ...
```

`subset_from_list` is now trivially safe: `BenchmarkConfig` has no subprocess handles — everything is a Pydantic model field, safe to `model_copy`.

`Benchmark` (the runtime object) stays as today, minus `subset_from_list`.

### task_metadata must exist before make()

The contract: **`task_metadata` is available at config construction time**, without calling `make()` or `setup()`.

For most benchmarks this is already true (static JSON loaded via `__init_subclass__`). For benchmarks with dynamic task lists (WorkArena), the right pattern is a developer script that writes `task_metadata.json` once and ships it with the package:

```
scripts/generate_task_metadata.py  →  runs get_all_tasks_agents() once
                                   →  writes task_metadata.json (committed to repo)

import time  →  task_metadata.json loaded automatically via __init_subclass__
```

This is what PR #292 (uniformization-workarena) does: all 333 tasks across L1, L2, and L3 are enumerated once, promoted to a typed `WorkArenaTaskMetadata` subclass, and shipped as a package resource. Level filtering is done by the user via `named_subset()`, not via a constructor argument.

Seeds are **not** stored in `task_metadata`. They come from a `SeedGenerator` (already in cube-standard) that lazily calls `get_all_tasks_agents()` on first use and caches `{task_id: [seeds]}`. The generator covers all three levels at once so it works naturally with any subset.

### make(infra_config)

```python
def make(self, infra_config: InfraConfig | None = None) -> "Benchmark":
    if infra_config is not None:
        for resource in self.resources:
            if infra_config.provision_status(resource) == "needs_provisioning":
                infra_config.provision(resource)          # L1: idempotent image provisioning
    return self._instantiate(infra_config)                # Benchmark is ready on return
```

`_instantiate()` constructs the `Benchmark`, which calls `_setup()` automatically in `model_post_init`. **`Benchmark.setup()` is not a public method** — a `Benchmark` is always born ready to use. There is no state where a `Benchmark` exists but hasn't been initialized. This eliminates the current footgun of constructing a benchmark and forgetting to call `.setup()`.

`infra_config=None` is valid for self-contained benchmarks (MiniWob starts its own HTTP server, no external deps).

### Serialization enables new patterns

Because `BenchmarkConfig` is pure data, you can:

```python
# Save a configured benchmark suite to disk and reload it exactly
composite = CompositeBenchmarkConfig([
    WorkArenaBenchmarkConfig().named_subset("l1").subset_from_list(my_tasks),
    MiniWobBenchmarkConfig(),
])
composite.model_dump_json()  # → fully serializable JSON

# Reload later, possibly on a different machine
config = CompositeBenchmarkConfig.model_validate_json(json_str)
benchmark = config.make(infra)
```

---

## Part 2 — CompositeBenchmark

### Problem

There is no way to run WorkArena + MiniWob + OSWorld in a single experiment today. Each benchmark needs its own `Experiment`, and there is no unified result.

### Design

`CompositeBenchmarkConfig` holds a list of `BenchmarkConfig` (or `BenchmarkPool`) instances. Because each element is a `BenchmarkConfig`, the whole thing is serializable.

```python
class CompositeBenchmarkConfig(BenchmarkConfig):
    sub_configs: list[BenchmarkConfig]  # can include BenchmarkPool instances

    def make(self, infra_config: InfraConfig | None = None) -> "CompositeBenchmark":
        sub_benchmarks = [c.make(infra_config) for c in self.sub_configs]
        return CompositeBenchmark(sub_benchmarks=sub_benchmarks)
```

`CompositeBenchmark` (the runtime object) iterates sub-benchmarks' `get_task_configs()` and routes each task to the right sub-benchmark's `_runtime_context`:

```python
class CompositeBenchmark(Benchmark):
    def get_task_configs(self) -> Generator[TaskConfig, None, None]:
        for i, b in enumerate(self.sub_benchmarks):
            for tc in b.get_task_configs():
                self._task_to_benchmark[tc.task_id] = i
                yield tc

    def _runtime_context_for(self, task_config: TaskConfig) -> RuntimeContext:
        idx = self._task_to_benchmark[task_config.task_id]
        return self.sub_benchmarks[idx]._runtime_context
```

### Serialization

The payoff of `BenchmarkConfig` being serializable:

```python
composite = CompositeBenchmarkConfig([
    BenchmarkPool(config=WorkArenaBenchmarkConfig().named_subset("l1"), n_servers=3),
    MiniWobBenchmarkConfig().subset_from_list(my_tasks),
    OSWorldBenchmarkConfig(),
])

# Save to disk — exact reproduction of this benchmark suite
composite.model_dump_json()   # → JSON

# Share with a colleague, reload on CI, store as experiment artifact
config = CompositeBenchmarkConfig.model_validate_json(json_str)
benchmark = config.make(azure_infra)
```

### Usage

```python
composite = CompositeBenchmarkConfig([
    BenchmarkPool(
        config=WorkArenaBenchmarkConfig().named_subset("l1"),
        infra=azure_infra,
        n_servers=3,
    ),
    MiniWobBenchmarkConfig(),
])
exp = Experiment(name="multi_bench", benchmark=composite.make(azure_infra), ...)
```

---

## Part 3 — BenchmarkPool

### Problem

WorkArena (ServiceNow) and WebArena degrade under load — roughly 7 concurrent agents per server. At RL scale (100s of rollouts), you need N server instances and dynamic load balancing.

### Design

`BenchmarkPool` accepts one `BenchmarkConfig` and calls `config.make(infra)` N times in the **main process**, each targeting a different server. This produces N live `Benchmark` instances — one per server slot — that stay in the main process for their entire lifetime.

`BenchmarkConfig.make()` is the key: without it there is no clean way to instantiate N identical benchmarks from one config. `InfraConfig` is responsible for handing out N distinct server endpoints on successive `make()` calls (e.g. from a pool of pre-provisioned ServiceNow instances).

#### What crosses the process boundary

`Benchmark` instances **never** leave the main process. They hold subprocess handles, browser connections, and other OS-level state that cannot be serialized to Ray workers. Only **`RuntimeContext` dicts** (plain Python dicts) cross the process boundary — they are exactly what `task_config.make(runtime_context=ctx)` already expects.

`LoadBalancerActor` holds a list of `RuntimeContext` dicts (one per slot), not `Benchmark` references. It hands out a context dict when a slot is free and reclaims it when the task finishes.

#### Dispatch flow

1. `run_with_ray` dispatches all N tasks to Ray immediately (non-blocking). Ray's scheduler queues them normally.
2. Each Ray worker, inside the `run_episode` remote function, calls `actor.acquire()` on the `LoadBalancerActor` — this blocks until a slot is free and returns the corresponding `RuntimeContext` dict.
3. The worker calls `task_config.make(runtime_context=ctx)` with that dict, runs the episode, then calls `actor.release(slot_idx)`.
4. `Episode` itself is unchanged — slot acquisition lives in the `run_episode` Ray wrapper, not inside `Episode`.

The actor handle is the only non-dict object that crosses process boundaries. Ray actor handles are natively serializable; `run_with_ray` passes it to each `run_episode.remote(episode, lb_actor=handle)` call.

#### LoadBalancerActor

The actor holds one `RuntimeContext` dict per slot and is the sole gatekeeper for those dicts. Because Ray actors process calls one at a time, `acquire()` / `release()` are race-free with no additional locking.

```python
@ray.remote
class LoadBalancerActor:
    def __init__(self, contexts: list[RuntimeContext]):
        # One RuntimeContext dict per server slot — plain dicts, fully serializable.
        self._contexts = contexts
        self._free = list(range(len(contexts)))

    def acquire(self) -> tuple[int, RuntimeContext]:
        """Block until a slot is free; return (slot_idx, context)."""
        while not self._free:
            time.sleep(0.05)   # or use async actor pattern to avoid spinning
        idx = self._free.pop(0)
        return idx, self._contexts[idx]

    def release(self, slot_idx: int) -> None:
        self._free.append(slot_idx)
```

### Usage

```python
# WorkArena loads all 333 tasks (L1+L2+L3) at import time.
# Level filtering is done via named_subset(), not via a constructor arg.
pool = BenchmarkPool(
    config=WorkArenaBenchmarkConfig().named_subset("l1"),
    infra=AzureInfraConfig(...),
    n_servers=3,
)
exp = Experiment(name="workarena-l1", benchmark=pool, ...)
run_with_ray(exp, n_cpus=21)    # 3 servers × 7 agents each
```

---

## Part 4 — ResourcePool (RL-scale, future)

For task-scoped resources (OSWorld VMs, per-task containers), each task currently pays a ~60s VM launch cost. A `ResourcePool` pre-warms N VMs and recycles them via snapshot revert.

This is a follow-on concern that doesn't depend on `BenchmarkConfig` — it's a Ray Actor that sits between `Experiment` and `InfraConfig`. Keeping it out of scope here to avoid conflation.

The shape is documented in the prior design (`docs/BENCHMARK_POOL_DESIGN.md`, section 3). The main open question — `ResourceHandle.revert_snapshot()` in cube-standard vs infra-specific — can be resolved independently.

---

## What Changes Where

### cube-standard

| Change | Detail |
|---|---|
| New `BenchmarkConfig` base class | Pure-data counterpart to `Benchmark`. Holds `task_metadata`, `resources`, `default_tool_config`, `seed_generator`. Defines `subset_from_list()` (trivial filter) and abstract `make(infra)`. |
| `Benchmark.subset_from_list()` removed | Lives on `BenchmarkConfig` now. |
| `Experiment` accepts `BenchmarkConfig` | Calls `config.make(infra)` internally, or user calls it before passing in. |

### cube-harness

| Change | Detail |
|---|---|
| Each cube: add `XxxBenchmarkConfig` | Rename current `XxxBenchmark` → `XxxBenchmarkConfig`, add `make()`. Keep `XxxBenchmark` as the (leaner) runtime class. |
| New `BenchmarkPool` | `src/cube_harness/benchmark_pool.py`. Calls `config.make(infra)` N times. |
| New `CompositeBenchmarkConfig` | `src/cube_harness/composite_benchmark.py`. Serializable list of `BenchmarkConfig`. |
| New `CompositeBenchmark` | Runtime object; routes tasks to sub-benchmark `_runtime_context`. |
| `run_with_ray` wiring | Pass `lb_actor` handle to each `run_episode.remote()` call when running with a `BenchmarkPool`; slot acquisition happens in the Ray wrapper, not in `Episode`. |

### Per-cube effort

| Cube | task_metadata contract | Level/subset filtering | Effort |
|---|---|---|---|
| WorkArena | Shipped JSON (all L1+L2+L3, 333 tasks). Regenerated by `scripts/generate_task_metadata.py`. PR #292. | `named_subset("l1")`, `named_subset("l2").subset_from_glob("in_human_curriculum", "True")` | **Trivial** (once PR #292 merges) |
| MiniWob | Shipped JSON (125 tasks, static) | `subset_from_list(my_tasks)` | **Trivial** |
| SWE-bench Verified | Shipped JSON. `install()` populates per-task execution cache from HuggingFace. PR #276. | `subset_from_list()` | **Trivial** (once PR #276 merges) |
| Terminal-Bench | Shipped JSON. `install()` populates dataset cache. PR #278. | `subset_from_list()` | **Trivial** (once PR #278 merges) |
| Arithmetic | Hardcoded ClassVar | N/A | **Trivial** |
| OSWorld | JSON from `install()` | `subset_from_glob()` | **Small** |
| WebArena-Verified | Lazy API call in `model_post_init` | `subset_from_list()` | **Medium** (needs `install()` to write JSON) |
| SWE-bench Live | Live GitHub issues | — | **Medium** (design decision: static snapshot vs live) |

For all "trivial" cubes the change is mechanical: rename `XxxBenchmark` → `XxxBenchmarkConfig`, slim down `XxxBenchmark` to hold only runtime state, add a `make()` method.

---

## Relationship to Nic's Uniformization PRs

The following PRs are direct prerequisites — they land the `task_metadata.json` contract that `BenchmarkConfig` requires:

| PR | Cube | What it does |
|---|---|---|
| #276 `uniformization-swebench` | SWE-bench Verified | Shipped `task_metadata.json`, `install()` for HuggingFace execution cache |
| #278 `uniformization-terminalbench` | Terminal-Bench | Same pattern |
| #292 `uniformization-workarena` | WorkArena | Shipped JSON (all 3 levels, 333 tasks), typed `WorkArenaTaskMetadata`, `named_subsets` for L1/L2/L3, `_setup()` reduced to a log line |

Once these merge, per-cube migration to `BenchmarkConfig` is purely mechanical for all "trivial" cubes. WorkArena in particular will already have the correct design: no `level` constructor arg, all levels loaded at import time, level filtering via `named_subset("l1")` in user-land.

---

## Open Questions

1. **`BenchmarkConfig` in cube-standard or cube-harness?**  
   It belongs in cube-standard (same layer as `TaskConfig`). `BenchmarkPool` and `CompositeBenchmark` are harness-specific.

2. **Backwards compatibility for `Benchmark.setup()`**  
   `setup()` becomes an implementation detail — called automatically in `model_post_init`, not exposed publicly. Current recipes that call `benchmark.setup()` explicitly need to migrate to `config.make(infra)`. Transition path: deprecate public `setup()` for 1–2 releases before removal.

3. **`InfraConfig` in `make()` vs passed at experiment time**  
   Should `infra_config` be passed to `make()` or stored on the config? Passing at `make()` is cleaner — the config describes *what*, not *where* to run it.

4. **`run_episode` wrapper and slot acquisition**  
   Slot acquisition for `BenchmarkPool` happens inside the `run_episode` Ray remote function, not inside `Episode`. This keeps `Episode` unchanged and avoids serializing `Benchmark` objects to workers. The open question is whether `run_with_ray` should detect a `BenchmarkPool` and inject the actor handle automatically, or whether `BenchmarkPool` exposes a `prepare_runner()` hook that wraps the remote function.
