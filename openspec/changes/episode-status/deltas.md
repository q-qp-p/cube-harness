# Deltas — Episode Status File

Applies to: `openspec/specs/episode/spec.md`, `openspec/specs/experiment/spec.md`,
`openspec/specs/storage/spec.md`

---

## episode/spec.md

### ADDED — `EpisodeStatus` dataclass

```python
@dataclass
class EpisodeStatus:
    status: Literal["RUNNING", "COMPLETED", "FAILED", "CANCELLED", "STALE"]
    task_id: str
    episode_id: int
    started_at: float
    ended_at: float | None = None
    last_heartbeat_at: float | None = None
    reward: float | None = None
    had_step_errors: bool = False
    retry_count: int = 0
```

### MODIFIED — `Episode._run_loop` lifecycle

Before (no status file):
- Trajectory written incrementally; no terminal signal on crash.

After:
1. Write `status=RUNNING` via `storage.write_episode_status()` before `setup_fn()`
2. Start background heartbeat thread (30s interval, updates `last_heartbeat_at`)
3. In `finally`: stop heartbeat thread; write `status=COMPLETED` or `status=FAILED`

### INVARIANT

`status.json` is always written before any step file. A missing `status.json` means
the worker died before the episode could initialise — treat as retriable.

---

## experiment/spec.md

### REMOVED — `_is_trajectory_successful`, `_load_successful_trajectory_ids`

Replaced by `_load_episode_statuses` which reads only `status.json` per episode
directory. No trajectory deserialization in the retry decision path.

### MODIFIED — Resume / retry semantics table

| `resume` | `retry_failed` | Episodes returned |
|----------|----------------|-------------------|
| False    | False          | All episodes from scratch |
| True     | False          | Episodes with no `status.json` (never started) |
| False    | True           | Episodes with `status IN (FAILED, STALE, CANCELLED)` or missing `status.json`, AND `retry_count < max_retries` |
| True     | True           | Union of the above two |

### ADDED — `max_retries: int = 3` field on `Experiment`

Controls how many times a failed episode is retried. Episodes at `retry_count >= max_retries`
are reported as permanently failed and excluded from future runs.

### ADDED — `max_retry_rounds: int = 1` parameter on `run_with_ray` and `run_sequentially`

After the main run completes, the runner checks for retriable episodes. If any exist
and `retry_rounds < max_retry_rounds`, it runs again with `retry_failed=True` on the
same output directory. The loop repeats until no retriable episodes remain or
`max_retry_rounds` is exhausted. The final `ExpResult` aggregates all rounds.

This allows a single `run_with_ray(exp)` call to automatically recover from transient
failures without caller intervention.

---

## storage/spec.md

### ADDED — `Storage` Protocol methods

```python
def write_episode_status(self, trajectory_id: str, status: EpisodeStatus) -> None
    # Atomic: write to .tmp, then os.replace()

def read_episode_status(self, trajectory_id: str) -> EpisodeStatus | None
    # Returns None if status.json does not exist
```

### ADDED — `FileStorage` implementation

- `write_episode_status`: serialises `EpisodeStatus` to JSON, writes atomically via
  a `.tmp` sibling file + `os.replace()`.
- `read_episode_status`: reads `<episode_dir>/status.json`; returns `None` on missing file.

---

## exp_runner.py (not a spec file — implementation note)

After `ray.cancel(ref, force=True)`, write `status=CANCELLED` for that episode via
`storage.write_episode_status()`. Increment `retry_count` by reading the current
status first.

After `ray.shutdown()`, sweep all episode directories. For any episode still in
`RUNNING` state, check `last_heartbeat_at`:
- Fresh (within threshold) → leave as `RUNNING` (sequential edge case or overlap window)
- Stale or missing → write `status=STALE`; these will be retried on the next run

This sweep is the only reliable way to detect dead workers: the driver is the last
process alive after the Ray cluster shuts down.
