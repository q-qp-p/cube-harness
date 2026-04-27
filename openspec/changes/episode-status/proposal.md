# RFC: Episode Status File

## Problem

The current resume/retry logic in `experiment.py` determines whether an episode needs
re-running by loading and scanning its full trajectory for `StepError` entries. This
has three failure modes:

**1. Stale RUNNING with no heartbeat.**
If a Ray worker is killed (OOM, `ray.cancel(force=True)`, machine failure), no terminal
status is ever written. The trajectory may be partial or missing. The current code
catches the load failure silently and drops the episode — it neither retries nor counts
it as done.

**2. False positive on recovered errors.**
A `StepError` recorded mid-episode (e.g. a tool error that the harness caught and
turned into an error observation, then continued) causes `_is_trajectory_successful`
to return `False`, triggering a retry even though the episode completed normally.

**3. Status requires reading the payload.**
To check whether an episode is done, the entire trajectory must be deserialized.
At scale (thousands of episodes per experiment) this is expensive and fragile — a
single corrupt step file breaks the check for that episode.

## Proposed solution

Write a small `status.json` file in each episode directory. Status is the ground truth
for retry decisions. The trajectory is data; `status.json` is control.

### Episode statuses

| Status | Written by | Meaning |
|---|---|---|
| `RUNNING` | `episode._run_loop` at start | Episode has started; worker is alive |
| `COMPLETED` | `episode._run_loop` in finally | Loop ran to natural end (done or max_steps); reward is irrelevant |
| `FAILED` | `episode._run_loop` in finally | Unhandled exception propagated out of the run loop |
| `CANCELLED` | `exp_runner` after `ray.cancel(force=True)` | Explicitly timed out by the harness |

### `status.json` schema

```json
{
  "status": "COMPLETED",
  "task_id": "workarena.servicenow.create-incident",
  "episode_id": 3,
  "started_at": 1745000000.0,
  "ended_at": 1745000120.0,
  "last_heartbeat_at": 1745000118.0,
  "reward": 1.0,
  "had_step_errors": true,
  "retry_count": 0
}
```

Fields:
- `status` — one of the four values above
- `started_at` / `ended_at` — wall-clock timestamps (None if not reached)
- `last_heartbeat_at` — updated by a background thread every N seconds while RUNNING
- `reward` — final reward (None until COMPLETED)
- `had_step_errors` — True if any step recorded a StepError; informational only, does not affect retry
- `retry_count` — how many times this episode has been retried; capped at `max_retries` to prevent infinite loops

### Heartbeat

A background thread in `_run_loop` writes `last_heartbeat_at` to `status.json` every
30 seconds while the episode is running. On recovery:

- `status == RUNNING` and `now - last_heartbeat_at < threshold` (e.g. 2 minutes) → truly running, do not retry
- `status == RUNNING` and `last_heartbeat_at` is stale or missing → dead worker, treat as FAILED for retry purposes

This works for both local (sequential) and distributed (Ray) runs without requiring
access to the Ray dashboard or any external state.

### Retry logic

An episode is eligible for retry if:
1. `status.json` does not exist (worker died before writing `RUNNING`)
2. `status == RUNNING` and heartbeat is stale
3. `status == FAILED` or `status == CANCELLED`
4. `retry_count < max_retries` (proposed default: 3)

An episode is considered done (do not retry) if:
- `status == COMPLETED`

Note: `COMPLETED` with `reward == 0` is a legitimate agent failure, not a technical
failure. Retrying it wastes quota.

### Changes to `Storage`

Two new methods on `FileStorage` (and `Storage` Protocol):

```python
def write_episode_status(self, trajectory_id: str, status: EpisodeStatus) -> None
    # Atomic write: write to .tmp then rename

def read_episode_status(self, trajectory_id: str) -> EpisodeStatus | None
    # Returns None if file does not exist
```

`EpisodeStatus` is a small dataclass (not a full Pydantic model — it must be writable
before the trajectory object exists).

Atomic write (write-then-rename) ensures a reader never sees a partial file.

### Changes to `Experiment`

`_is_trajectory_successful` and `_load_successful_trajectory_ids` are replaced by:

```python
def _load_completed_trajectory_ids(self, storage: FileStorage) -> set[str]:
    # Reads only status.json per episode directory — no trajectory deserialization
```

### Changes to `exp_runner`

After `ray.cancel(force=True)`, write `status == CANCELLED` for that episode.

## Alternatives considered

**Ray dashboard query.** Store the Ray job ID and query the dashboard to check if it
is still running. Rejected: couples status check to Ray infrastructure, does not work
for sequential runs, requires network access.

**PID file.** Store the worker PID and check if the process is alive. Rejected: PIDs
are recycled; unreliable on multi-node clusters.

**Trajectory existence as sentinel.** Current approach — treat any trajectory as done.
Rejected: requires full deserialization, drops silently-failed episodes.

## Scope

Touches: `episode.py`, `experiment.py`, `exp_runner.py`, `storage.py`, `storage` spec,
`episode` spec, `experiment` spec.

Does not change: trajectory format, `Trajectory` model, existing step files, XRay viewer.

## Open questions

1. What should `max_retries` default to? Proposed: 3.
2. Heartbeat interval: 30s? Staleness threshold: 2 minutes?
3. Should `CANCELLED` be retried by default, or only on explicit opt-in?
