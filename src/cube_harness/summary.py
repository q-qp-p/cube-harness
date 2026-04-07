import json
import time
from pathlib import Path

from cube.core import EnvironmentOutput

from cube_harness.core import AgentOutput, Trajectory, TrajectoryStep


class SummaryProcessor:

    def __init__(self, episode_dir: Path) -> None:
        self._summary_path = episode_dir / "episode_summary.jsonl"
        self._running: dict = {
            "n_env_steps": 0,
            "n_agent_steps": 0,
            "total_actions": 0,
            "total_llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost": 0.0,
            "reward": 0.0,
            "done": False,
        }

    def on_step(self, step_num: int, step: TrajectoryStep) -> None:
        if isinstance(step.output, AgentOutput):
            self._running["n_agent_steps"] += 1
            self._running["total_actions"] += len(step.output.actions)
            self._running["total_llm_calls"] += len(step.output.llm_calls)
            for llm_call in step.output.llm_calls:
                if llm_call.usage:
                    self._running["prompt_tokens"] += llm_call.usage.prompt_tokens
                    self._running["completion_tokens"] += llm_call.usage.completion_tokens
                    self._running["cost"] += llm_call.usage.cost
        elif isinstance(step.output, EnvironmentOutput):
            self._running["n_env_steps"] += 1
            self._running["reward"] = step.output.reward
            self._running["done"] = step.output.done

        entry = {**self._running, "step_num": step_num, "timestamp": time.time()}
        with open(self._summary_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def on_episode_complete(self, trajectory: Trajectory, storage) -> None:
        storage.update_experiment_summary(trajectory)
