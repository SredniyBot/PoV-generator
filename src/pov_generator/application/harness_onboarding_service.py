"""Онбординг harness-агентов (Ф4): от «чистой системы» до «агент работает».

Делает подготовку видимой и управляемой (никаких «тихих» долгих операций):
- готовность Docker (индикатор + подсказка);
- подготовка образа с прогрессом (фоновая, статус опрашивается);
- самопроверка (тривиальный прогон в песочнице — доказывает рабочую цепочку);
- рекомендации по мощности машины (из Ф3-калибровки).

Без Docker всё деградирует мягко: статус «недоступен», prepare/self-test
возвращают понятную ошибку, ядро/LLM-путь не затронуты.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..infrastructure.harness import (
    ExpectedArtifact,
    HarnessCapacity,
    HarnessRunSpec,
    RunLimits,
    SandboxRuntime,
    detect_host_capacity,
)
from ..infrastructure.harness.docker_env import DockerStatus, probe_docker
from ..infrastructure.harness.images import ImagePreparer
from ..infrastructure.harness.providers.command import CommandHarnessProvider

# Образ и команда самопроверки: лёгкий публичный образ + запись результата по
# соглашению. Реальные образы агентов придут в Ф7; здесь — проверка цепочки.
DEFAULT_SELF_TEST_IMAGE = "busybox:latest"
SELF_TEST_ROLE = "selftest"
_SELF_TEST_COMMAND = (
    "mkdir -p /work/.povgen/out && "
    'printf \'{"ok": true}\' > /work/.povgen/out/selftest.json'
)


@dataclass(frozen=True)
class SelfTestResult:
    """Итог самопроверки агента."""

    ok: bool
    duration_ms: int
    transcript: str = ""
    error: str | None = None


@dataclass(frozen=True)
class PullProgress:
    """Состояние подготовки образа (для наглядного прогресса)."""

    image: str
    in_progress: bool
    ready: bool
    status: str | None = None
    progress: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class HarnessReadiness:
    """Сводная готовность подсистемы агентов — один статус для UI."""

    docker: DockerStatus
    capacity: HarnessCapacity
    default_image: str
    image_ready: bool
    pull: PullProgress | None = None
    # Кратко: можно ли реально запускать агентов прямо сейчас.
    ready: bool = False
    blockers: tuple[str, ...] = field(default_factory=tuple)


class HarnessOnboardingService:
    def __init__(
        self,
        sandbox_runtime: SandboxRuntime,
        image_preparer: ImagePreparer,
        *,
        self_test_image: str = DEFAULT_SELF_TEST_IMAGE,
        docker_probe: Callable[[], DockerStatus] = probe_docker,
    ) -> None:
        self._sandbox = sandbox_runtime
        self._images = image_preparer
        self._self_test_image = self_test_image
        self._docker_probe = docker_probe
        self._pull: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # --- статус / готовность ------------------------------------------------

    def docker_status(self) -> DockerStatus:
        return self._docker_probe()

    def capacity(self) -> HarnessCapacity:
        return detect_host_capacity()

    def readiness(self) -> HarnessReadiness:
        docker = self.docker_status()
        image_ready = self._images.is_ready(self._self_test_image) if docker.available else False
        pull = self.pull_progress(self._self_test_image)
        blockers: list[str] = []
        if not docker.available:
            blockers.append("Docker недоступен")
        elif not image_ready:
            blockers.append("Образ агента не подготовлен")
        return HarnessReadiness(
            docker=docker,
            capacity=self.capacity(),
            default_image=self._self_test_image,
            image_ready=image_ready,
            pull=pull,
            ready=not blockers,
            blockers=tuple(blockers),
        )

    # --- подготовка образа (фоновая, с прогрессом) --------------------------

    def start_prepare(self, image: str | None = None) -> threading.Thread:
        """Запустить скачивание образа в фоне. Прогресс — через pull_progress."""
        target = image or self._self_test_image
        with self._lock:
            self._pull[target] = {
                "image": target,
                "in_progress": True,
                "ready": False,
                "status": "starting",
                "progress": None,
                "error": None,
            }
        thread = threading.Thread(target=self._prepare_blocking, args=(target,), daemon=True)
        thread.start()
        return thread

    def _prepare_blocking(self, image: str) -> None:
        def on_progress(line: Mapping[str, Any]) -> None:
            with self._lock:
                state = self._pull.get(image)
                if state is None:
                    return
                if line.get("status"):
                    state["status"] = str(line["status"])
                if isinstance(line.get("progress"), int):
                    state["progress"] = int(line["progress"])

        try:
            status = self._images.prepare(image, on_progress=on_progress)
            with self._lock:
                self._pull[image].update(
                    in_progress=False,
                    ready=status.ready,
                    error=status.error,
                    status="ready" if status.ready else (status.error or "failed"),
                )
        except Exception as exc:  # noqa: BLE001 — фон не должен падать наружу
            with self._lock:
                self._pull[image].update(
                    in_progress=False, ready=False, error=str(exc).strip() or type(exc).__name__
                )

    def pull_progress(self, image: str | None = None) -> PullProgress | None:
        target = image or self._self_test_image
        with self._lock:
            state = self._pull.get(target)
            if state is None:
                return None
            return PullProgress(
                image=target,
                in_progress=bool(state["in_progress"]),
                ready=bool(state["ready"]),
                status=state.get("status"),
                progress=state.get("progress"),
                error=state.get("error"),
            )

    # --- самопроверка -------------------------------------------------------

    def self_test(self, *, image: str | None = None, timeout_s: int = 60) -> SelfTestResult:
        """Тривиальный прогон в песочнице: доказывает, что цепочка рабочая."""
        target = image or self._self_test_image
        provider = CommandHarnessProvider(
            sandbox=self._sandbox,
            image=target,
            command=_SELF_TEST_COMMAND,
            name="selftest",
            default_timeout_s=timeout_s,
        )
        spec = HarnessRunSpec(
            brief="harness self-test",
            expected_artifacts=(ExpectedArtifact(role=SELF_TEST_ROLE, fmt="json"),),
            limits=RunLimits(wall_clock_s=timeout_s),
        )
        start = time.perf_counter()
        try:
            result = provider.run(spec)
        except Exception as exc:  # noqa: BLE001
            return SelfTestResult(
                ok=False,
                duration_ms=round((time.perf_counter() - start) * 1000),
                error=str(exc).strip() or type(exc).__name__,
            )
        duration_ms = round((time.perf_counter() - start) * 1000)
        ok = result.status == "completed" and any(
            a.role == SELF_TEST_ROLE for a in result.artifacts
        )
        return SelfTestResult(
            ok=ok,
            duration_ms=duration_ms,
            transcript=result.transcript,
            error=None if ok else (result.error or "самопроверка не дала результата"),
        )
