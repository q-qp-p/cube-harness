import logging
import urllib.error
import urllib.request
from collections.abc import Generator
from typing import ClassVar

from pydantic import PrivateAttr
from cube.benchmark import Benchmark, BenchmarkMetadata
from cube.resource import DockerServiceConfig, InfraConfig, ResourceHandle
from cube.task import TaskConfig
from webarena_verified.types.config import EnvironmentConfig, WebArenaVerifiedConfig
from webarena_verified.types.task import WebArenaSite

from cube.tool import ToolboxConfig

from webarena_verified_cube.task import WebArenaVerifiedTaskConfig, WebArenaVerifiedTaskMetadata
from webarena_verified_cube.tool import HarPlaywrightConfig, SubmitResponseConfig

logger = logging.getLogger(__name__)


class WebArenaVerifiedBenchmark(Benchmark):
    """WebArena Verified — 812 verified web automation tasks across 6 platforms.

    task_metadata.json is a shipped package resource containing lightweight public fields
    (sites, expected_action, intent_template_id). No heavy execution data exists — all
    task information is available from the webarena-verified library at runtime.

    Filtering is done in user-land via subset_from_glob() / subset_from_list():
        bench.subset_from_glob("sites", "*shopping_admin*")
        bench.subset_from_glob("expected_action", "RETRIEVE")
        bench.subset_from_list(["0", "1", "5"])

    To regenerate task_metadata.json (developer use only), run:
        scripts/generate_task_metadata.py
    """

    benchmark_metadata: ClassVar[BenchmarkMetadata] = BenchmarkMetadata(
        name="webarena-verified-cube",
        version="1.0.0",
        description="WebArena-Verified benchmark — 812 verified web automation tasks across 6 platforms",
        num_tasks=812,
        tags=["browser", "web", "ui", "webarena"],
    )
    task_metadata: ClassVar[dict[str, WebArenaVerifiedTaskMetadata]]  # type: ignore - populated automatically at import time in Benchmark.__init_subclass__
    task_config_class: ClassVar[type[TaskConfig]] = WebArenaVerifiedTaskConfig

    default_tool_config: ToolboxConfig = ToolboxConfig(tool_configs=[HarPlaywrightConfig(), SubmitResponseConfig()])  # type: ignore

    wav_config: WebArenaVerifiedConfig = WebArenaVerifiedConfig()
    """WAV client configuration.  When ``infra`` is set, ``environments`` is populated
    automatically after launch and the default is sufficient.  When ``infra=None``,
    ``environments`` must be set manually before tasks will work (``render_url`` raises
    if it is ``None``).
    """

    infra: InfraConfig | None = None
    """When set, provision (if needed) + launch happens automatically in setup().

    Pass the ``DockerServiceConfig`` via ``resources=[...]`` at construction time.
    The handle's ``endpoints`` are translated to ``wav_config.environments`` using
    ``resource.endpoint_to_site``.  If ``None``, the benchmark behaves exactly as
    before: ``wav_config.environments`` must be populated manually.
    """

    _handle: ResourceHandle | None = PrivateAttr(default=None)

    def _setup(self) -> None:
        if self.infra is not None:
            self._handle = self._launch_and_configure()
            return

        # Legacy path: verify manually-configured URLs are reachable.
        if self.wav_config.environments is None:
            return
        for site, env_config in self.wav_config.environments.items():
            url = env_config.active_url
            if url is None:
                continue
            try:
                urllib.request.urlopen(url, timeout=5)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise RuntimeError(
                    f"Cannot reach {site} at {url}. "
                    f"Start the Docker container with: `webarena-verified env start --site {site.value}`"
                ) from e

    def _launch_and_configure(self) -> ResourceHandle:
        """Provision (if needed) + launch the resource and populate self.wav_config."""
        assert self.infra is not None
        docker_resources = [r for r in self.resources if isinstance(r, DockerServiceConfig)]
        if not docker_resources:
            raise ValueError(
                "infra= is set but no DockerServiceConfig found in resources=. "
                "Pass the DockerServiceConfig as resources=[...] at construction time."
            )
        if len(docker_resources) > 1:
            raise ValueError(
                f"infra= only supports a single DockerServiceConfig, but {len(docker_resources)} were found: "
                f"{[r.name for r in docker_resources]}. "
                "Pass exactly one DockerServiceConfig in resources=."
            )
        # Unpack the single validated item — tuple unpacking enforces the invariant at runtime.
        (resource,) = docker_resources

        if self.infra.provision_status(resource) == "needs_provisioning":
            logger.info("Provisioning %r on %s …", resource.name, self.infra.fingerprint())
            self.infra.provision(resource)

        logger.info("Launching %r …", resource.name)
        handle = self.infra.launch(resource)
        try:
            self._configure_wav_from_handle(handle, resource)
        except Exception:
            handle.close()
            raise
        return handle

    def _configure_wav_from_handle(self, handle: ResourceHandle, resource: DockerServiceConfig) -> None:
        """Translate handle.endpoints → wav_config.environments via resource.endpoint_to_site."""
        if not resource.endpoint_to_site:
            raise ValueError(
                f"DockerServiceConfig {resource.name!r} has no endpoint_to_site mapping. "
                "Add endpoint_to_site={'service_key': 'webarena_site_value', ...} to the resource."
            )
        environments: dict[WebArenaSite, EnvironmentConfig] = {}
        for service_name, url in handle.endpoints.items():
            site_value = resource.endpoint_to_site.get(service_name)
            if site_value is None:
                continue
            try:
                site = WebArenaSite(site_value)
            except ValueError:
                raise ValueError(
                    f"endpoint_to_site maps {service_name!r} → {site_value!r} which is not a valid WebArenaSite. "
                    f"Valid values: {[s.value for s in WebArenaSite]}"
                )
            if site not in environments:
                environments[site] = EnvironmentConfig(urls=[url])

        if not environments:
            raise ValueError(
                f"handle.endpoints {list(handle.endpoints)!r} produced no WebArenaSite entries. "
                "Check resource.endpoint_to_site."
            )
        self.wav_config = self.wav_config.model_copy(update={"environments": environments})
        logger.info(
            "wav_config.environments: %s",
            {s.value: e.active_url for s, e in environments.items()},
        )

    def close(self) -> None:
        if self._handle is not None:
            logger.info("Closing infra handle for run_id=%s", self._handle.run_id[:8])
            self._handle.close()
            self._handle = None

    def get_task_configs(self) -> Generator[WebArenaVerifiedTaskConfig, None, None]:
        """Yield TaskConfigs with wav_config forwarded from benchmark settings."""
        for tm in self.task_metadata.values():
            yield WebArenaVerifiedTaskConfig(
                task_id=tm.id,
                tool_config=self.default_tool_config,
                wav_config=self.wav_config,
            )
