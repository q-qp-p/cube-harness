# EvalLog

**Module:** `cube_harness.eval_log`

## Purpose

Exports one structured evaluation record per completed episode. Records are appended
to `<output_dir>/eval_log.jsonl` — a plain JSONL file readable without any
cube-harness dependency.

The primary consumer is **Project ATLAS** (Agent-Task Latent Analysis System), which
builds the community matrix **M[agent, task] = reward** from these records via sparse
matrix factorization and IRT. Secondary consumers include leaderboards, cost trackers,
and any framework that wants a stable per-episode data contract.

EEE compatibility target: fields are named and structured to map cleanly to the
[Every Eval Ever](https://github.com/evaleval/every_eval_ever) instance-level schema.
When the EEE agentic extension (Elron's PR #70) is finalized, a small rename-and-nest
migration produces a valid EEE record. See the [EEE compatibility map](#eee-compatibility-map).

---

## Public API

### `UsageSummary`

```python
class UsageSummary(TypedBaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    n_llm_calls: int = 0

    @classmethod
    def from_summary_stats(cls, stats: dict | None) -> "UsageSummary"
```

Aggregated LLM token usage and cost across a complete episode. Built from
`Trajectory.summary_stats` — no re-scanning of steps required.

---

### `AgentInfo`

```python
class AgentInfo(TypedBaseModel):
    # Identity
    agent_id: str                        # SHA-256(sorted config JSON)
    config_type: str                     # AgentConfig._type discriminator
    config: dict                         # full serialized agent config
    llm_model: str | None                # extracted from config

    # Capabilities (episode-specific)
    tools: list[dict]                    # litellm function-call format
    tool_names: list[str]                # names only, derived from tools

    # Runtime environment
    framework_version: str               # cube-harness version
    dependency_versions: dict[str, str]  # 9 tracked packages

    # Git provenance
    git_commit: str | None
    git_remote_url: str | None           # permanent GitHub permalink
    git_is_dirty: bool | None

    # LLM warm-start embedding (ATLAS cold-start)
    description: str | None

    @classmethod
    def from_agent_config(
        cls,
        agent_config: AgentConfig,
        action_schemas: list[dict] | None = None,
        git_cwd: str | None = None,
    ) -> "AgentInfo"

    def with_action_schemas(self, action_schemas: list[dict]) -> "AgentInfo"
    # Returns a copy with tools + tool_names replaced. Used by export_eval_log()
    # to inject per-episode action schemas without re-running from_agent_config().
```

**`agent_id`** is the primary stable row key for the ATLAS matrix. It is the
SHA-256 of the agent config serialized to JSON with sorted keys. Two runs of the
same config produce the same `agent_id`, regardless of wall time or machine.

**`tools` / `tool_names`** are episode-specific: the same agent receives different
action schemas on different tasks. These fields capture the actual action set available
during this episode, read from `trajectory.metadata["action_schemas"]` (written by
`Episode._run_loop`). They are NOT derived from the agent config alone.

**`description`** is optional free-form prose intended for ATLAS's LLM warm-start
embedding (cold-start for new agents with zero observed scores). May be human-authored
or synthesized from the structured fields above.

Tracked packages: `cube-harness`, `cube`, `litellm`, `anthropic`, `openai`,
`browsergym-core`, `playwright`, `pydantic`, `ray`.

---

### `TaskInfo`

```python
class TaskInfo(TypedBaseModel):
    # Benchmark identity
    benchmark_id: str                    # slugified lowercase name
    benchmark_name: str
    benchmark_version: str | None
    benchmark_description: str | None
    benchmark_authors: list[str]
    benchmark_tags: list[str]

    # Task identity
    task_id: str
    task_version_hash: str | None        # SHA-256 of TaskConfig JSON
    seed: int | None
    split: str | None                    # "train" | "val" | "test"

    # Task description (layered)
    abstract_description: str | None     # broad category — not the agent's goal
    first_observation_text: str | None   # actual text the agent saw at runtime
    recommended_max_steps: int | None
    extra_info: dict[str, Any]           # TaskMetadata.extra_info passthrough

    @classmethod
    def from_trajectory_and_metadata(
        cls,
        trajectory: Trajectory,
        benchmark_metadata: BenchmarkMetadata | None = None,
        task_metadata: TaskMetadata | None = None,
        task_config: TaskConfig | None = None,
    ) -> "TaskInfo"
```

**`task_version_hash`** is the SHA-256 of `TaskConfig.model_dump_json(serialize_as_any=True)`.
It changes whenever the task config changes, even if `task_id` is unchanged. ATLAS uses
it to detect silent benchmark drift: if the same `task_id` has two different hashes across
submissions, the records cannot be naively merged in the matrix.

**`first_observation_text`** is extracted from the first `EnvironmentOutput` in the
trajectory — the actual text the agent received at runtime. This is distinct from
`input.raw` in EEE (the static eval spec): for GUI/web tasks the first observation
includes live UI state (AXTree, HTML) that differs from the benchmark's written description.
This is the primary field for ATLAS task embedding.

**`abstract_description`** comes from `TaskMetadata.abstract_description`. It is
broad and not task-instance-specific — used for searching and filtering, not embedding.

---

### `TaskEvalRecord`

```python
class TaskEvalRecord(TypedBaseModel):
    # Descriptors
    task: TaskInfo
    agent: AgentInfo

    # Outcome
    success: bool                        # reward > 0
    reward: float                        # scalar final reward
    reward_breakdown: dict               # full reward_info (sub-goals, done flag, etc.)
    error_type: str | None               # exception class name if any step errored

    # Trajectory summary
    n_steps: int                         # total steps (agent + env)
    n_agent_steps: int                   # agent decision turns
    n_env_steps: int                     # env executions
    wall_time_s: float | None
    usage: UsageSummary

    # Provenance
    run_id: str                          # "{exp_name}_{trajectory_id}"
    trajectory_id: str
    timestamp: float                     # episode start, Unix
    framework_version: str

    # MNAR bias correction
    declaration: dict                    # see Declaration contract below

    @classmethod
    def from_trajectory(
        cls,
        trajectory: Trajectory,
        agent_info: AgentInfo,
        task_info: TaskInfo,
        exp_name: str = "",
    ) -> "TaskEvalRecord"
```

The **`declaration`** field carries MNAR (Missing Not At Random) correction metadata
required for ATLAS submissions. See [Declaration contract](#declaration-contract) below.

**`error_type`** is the exception class name (e.g. `"TimeoutError"`) of the first
`StepError` found in the trajectory. It is `None` for clean episodes that simply scored
zero — a zero-reward, no-error episode is a valid failure, not a crash.

---

### `EvalLog`

```python
class EvalLog(TypedBaseModel):
    records: list[TaskEvalRecord] = []

    def save_jsonl(self, path: Path) -> None
    # Writes all records to JSONL (one JSON object per line).

    @classmethod
    def load_jsonl(cls, path: Path) -> "EvalLog"
    # Loads all records from JSONL.

    @staticmethod
    def append_record(record: TaskEvalRecord, path: Path) -> None
    # Appends a single record (streaming mode). Safe for one writer;
    # not safe for concurrent multi-process writes without external file locking.
```

The JSONL format is the canonical wire format. Each line is a self-contained
`TaskEvalRecord` — no schema header, no envelope, no cube-harness dependency to read.

---

## Declaration Contract

`TaskEvalRecord.declaration` is a `dict` with three optional fields:

```jsonc
{
  "motivation": "capability_probe",       // why this run was submitted
  "task_selection_method": "random",      // how tasks were chosen
  "compute_budget": "full_benchmark"      // how much was run
}
```

**Allowed values:**

| Field | Values |
|---|---|
| `motivation` | `"capability_probe"` · `"leaderboard"` · `"training_data"` · `"debugging"` |
| `task_selection_method` | `"random"` · `"difficulty_stratified"` · `"domain_filtered"` · `"cherry_picked"` |
| `compute_budget` | `"full_benchmark"` · `"partial"` · `"targeted"` |

**Why it exists.** The ATLAS matrix is extremely sparse and Missing Not At Random (MNAR):
labs cherry-pick which (agent, task) pairs they run, gravitating toward benchmarks where
their agent is strong and avoiding embarrassing failures. If the matrix factorization
trains on this biased data naively, recovered latent difficulty vectors reflect selection
bias, not true task difficulty.

Declaration fields feed a propensity model `P(observed | agent, task, declaration)`.
Observations from cherry-picked runs are downweighted via Inverse Propensity Scoring;
random-selection runs carry full weight. The anchor set (a fixed task subset every ATLAS
submitter must evaluate) handles the mandatory portion cleanly; declaration corrects the
free-choice portion.

For non-ATLAS use the field can be omitted (`{}`). ATLAS community submissions must
populate all three fields.

---

## Integration Points

### `Episode._run_loop` → `trajectory.metadata["action_schemas"]`

Immediately after `agent = self.config.agent_config.make(action_set)`:

```python
action_schemas = [a.as_dict() for a in action_set]
```

Added to `trajectory.metadata`:

```python
metadata={
    "task_id": task_id,
    "agent_name": agent_name,
    "action_schemas": action_schemas,
    **env_output.info,
}
```

This is the only change to the episode loop. Action schemas are captured once at
task reset time and persisted so `export_eval_log()` can reconstruct `AgentInfo.tools`
without re-instantiating tasks.

### `Experiment.export_eval_log`

```python
def export_eval_log(
    self,
    output_path: Path | None = None,
    git_cwd: str | None = None,
) -> EvalLog
```

Called after `run_sequentially()` or `run_with_ray()` completes. Iterates
`storage.list_trajectory_ids()`, builds one `TaskEvalRecord` per trajectory, writes
`<output_dir>/eval_log.jsonl` (or `output_path` if provided). Returns the in-memory
`EvalLog`.

Reads all data from persisted files. No task or benchmark re-instantiation required.

**Resolution order for `AgentInfo.tools`:**
`trajectory.metadata["action_schemas"]` → `[]` if absent (old trajectory without the field).

**Resolution order for `TaskInfo` fields:**
`benchmark.benchmark_metadata` → trajectory metadata fallback → `"unknown"`.

---

## On-disk output

```
<output_dir>/
├── experiment_config.json
├── experiment_summary.json
├── eval_log.jsonl              ← one JSON line per completed episode
└── episodes/
    └── <trajectory_id>/
        ├── episode_config.json
        └── ...
```

`eval_log.jsonl` is written by `export_eval_log()` after the experiment completes.
It is not written incrementally during the run; call `EvalLog.append_record()` directly
for streaming use cases.

---

## EEE Compatibility Map

Fields are named to make migration to the
[EEE instance-level schema](https://github.com/evaleval/every_eval_ever) a
rename-and-nest. No field needs to be dropped or recomputed.

| EvalLog field | EEE destination | Notes |
|---|---|---|
| `task.benchmark_name` | `evaluation_name` | |
| `task.task_id` | `sample_id` | open: semantic name vs numeric key |
| `task.task_version_hash` | `sample_hash` | EEE scope = input hash; ATLAS scope = full config hash |
| `task.seed` | `eval_conditions.seed` | Elron's agentic ext |
| `task.split` | `metadata.split` | string escape hatch |
| `task.benchmark_version` | `metadata.benchmark_version` | string escape hatch |
| `task.benchmark_tags` | `metadata.benchmark_tags` | comma-joined string |
| `task.first_observation_text` | new top-level field | needs EEE schema ext |
| `agent.*` | inside `agent_system.*` | Elron's agentic ext; rename fields |
| `reward` | `evaluation.score` | rename |
| `reward_breakdown` | `evaluation.breakdown` | nest inside `evaluation` |
| `usage.*` | `session_accounting.*` | Elron's agentic ext; rename fields |
| `declaration` | new top-level field | needs EEE schema ext |

Open questions pending Elron's response: see [proposal.md](../../changes/atlas-eval-log/proposal.md).

---

## Invariants

1. `agent_id` is deterministic: same `AgentConfig` → same hash, regardless of run time,
   machine, or framework version. Never include timestamps or random values in the config.
2. `task_version_hash` covers the full `TaskConfig` JSON, not just the prompt. A task
   whose environment setup changes (e.g. a Docker image bump) produces a new hash even
   if the written instructions are identical.
3. `first_observation_text` is extracted from the trajectory, not from the task spec.
   It reflects what the agent actually received, including any dynamic environment state
   injected at runtime.
4. JSONL lines are self-contained. Appending records to an existing file is safe; the
   file remains valid JSONL after partial writes.
5. `declaration` defaults to `{}`. ATLAS server rejects submissions with empty
   `declaration`; cube-harness does not enforce this locally.

## Gotchas

- `export_eval_log()` loads all trajectories into memory to build `AgentInfo` and
  `TaskInfo`. For experiments with thousands of episodes, this can be slow. Use
  `EvalLog.append_record()` directly inside a custom runner for streaming writes.
- Old trajectories (before the `action_schemas` metadata field was added) yield
  `AgentInfo.tools = []` and `AgentInfo.tool_names = []`. This is valid — records are
  still useful for reward/cost stats, just not for tool-level analysis.
- `git_is_dirty = True` means the eval may not reproduce exactly from `git_commit`
  alone. ATLAS should flag these records accordingly (lower confidence in provenance).
- `AgentInfo.description` is never auto-populated by `from_agent_config()`. Set it
  manually or via a post-processing step when preparing ATLAS submissions.
