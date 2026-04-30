import os
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from dotenv import load_dotenv

from cube_harness.experiment import EXP_DIR, make_experiment_output_dir

try:
    __version__ = version("cube-harness")
except PackageNotFoundError:
    __version__ = "unknown"


def setup_env(env_file: Path | None = None) -> None:
    """Normalize DOCKER_HOST and load .env credentials.

    Handles Podman's http+unix:// scheme (which the Docker SDK rejects) and
    ensures load_dotenv doesn't clobber the already-expanded value from the shell.

    Args:
        env_file: Explicit path to a .env file. If None, walks up from CWD to
                  find the nearest .env, falling back to ~/.env.
    """
    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host.startswith("http+unix://"):
        docker_host = re.sub(r"^http\+unix://", "unix://", docker_host)
    if docker_host:
        os.environ["DOCKER_HOST"] = docker_host

    if env_file is None:
        for parent in [Path.cwd(), *Path.cwd().parents]:
            candidate = parent / ".env"
            if candidate.exists():
                env_file = candidate
                break
        else:
            env_file = Path.home() / ".env"

    load_dotenv(env_file, override=True)

    # Re-apply after load_dotenv(override=True) to avoid clobbering the normalized value.
    if docker_host:
        os.environ["DOCKER_HOST"] = docker_host


__all__ = ["EXP_DIR", "__version__", "make_experiment_output_dir", "setup_env"]
