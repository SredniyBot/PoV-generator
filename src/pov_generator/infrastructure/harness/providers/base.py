"""Общая обвязка sandbox-harness'а (Ф7): провижн → посев → команда → гейты →
сбор → teardown.

Реальные адаптеры (Claude Code, Aider, generic) отличаются лишь ТРЕМЯ вещами:
команда запуска агента, (опц.) подготовка перед запуском и стратегия сбора
результата. Всё остальное — поднятие/снос песочницы, посев brief и входов,
прогон команды с таймаутом и стримом логов, гейты «готово» (DoD) — одно на всех.
Поэтому адаптер маленький: различия не текут в ядро.

Движок-агностично: c ``StubSandboxRuntime`` — тесты/CI без Docker, c
``DockerSandboxRuntime`` — реальный прогон.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..gates import run_gates
from ..protocol import HarnessRunResult, HarnessRunSpec, HarvestedArtifact
from ..sandbox import (
    ResourceLimits,
    SandboxHandle,
    SandboxRuntime,
    SandboxSpec,
    shell_argv,
)

_BRIEF_PATH = "/work/.povgen/brief.txt"
_OUT_DIR = "/work/.povgen/out"


class HarvestError(Exception):
    """Сбор результата не удался (агент не положил ожидаемый артефакт)."""


class SandboxHarnessProvider:
    """База адаптера: жизненный цикл прогона в песочнице.

    Подклассы переопределяют:
      * :meth:`_build_command` — argv запуска агента из ``spec``;
      * :meth:`_prepare` (опц.) — действия после посева до запуска (напр. git);
      * :meth:`_harvest` — собрать артефакты из песочницы после успешного
        прогона и пройденных гейтов.
    """

    def __init__(
        self,
        *,
        sandbox: SandboxRuntime,
        image: str,
        name: str,
        model: str | None = None,
        resource_limits: ResourceLimits | None = None,
        default_timeout_s: int | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._image = image
        self.name = name
        self.model = model
        self._resource_limits = resource_limits or ResourceLimits()
        self._default_timeout_s = default_timeout_s

    def run(self, spec: HarnessRunSpec) -> HarnessRunResult:
        handle = self._sandbox.provision(
            SandboxSpec(
                image=self._image,
                limits=self._resource_limits,
                workdir="/work",
                volume=spec.volume,
            )
        )
        logs: list[str] = []
        try:
            # 1. Посев: brief + входные артефакты как файлы.
            seed: dict[str, bytes] = {_BRIEF_PATH: spec.brief.encode("utf-8")}
            for filename, content in spec.inputs.items():
                seed[f"/work/{filename}"] = content.encode("utf-8")
            self._sandbox.put_files(handle, seed)

            # 2. Подготовка (опц.): напр. инициализация git-репозитория для
            #    diff-harvest. По умолчанию — ничего.
            self._prepare(handle, logs.append)

            # 3. Запуск агента.
            argv = self._build_command(spec)
            timeout_s = (
                spec.limits.wall_clock_s
                if spec.limits and spec.limits.wall_clock_s
                else self._default_timeout_s
            )
            result = self._sandbox.exec(
                handle, argv, timeout_s=timeout_s, on_log=logs.append
            )
            transcript = "".join(logs)
            if result.timed_out:
                return HarnessRunResult(
                    status="partial",
                    transcript=transcript,
                    error=f"Прогон прерван по таймауту ({timeout_s} c).",
                )
            if result.exit_code != 0:
                return HarnessRunResult(
                    status="failed",
                    transcript=transcript,
                    error=f"Команда агента вернула код {result.exit_code}.",
                )

            # 4. Гейты «готово» (DoD): проверяем результат в той же песочнице
            #    ДО сбора. Провал любого = узел не достиг готовности.
            gate_results = run_gates(
                self._sandbox, handle, spec.gates, on_log=logs.append
            )
            transcript = "".join(logs)
            failed_gates = [g for g in gate_results if not g.passed]
            if failed_gates:
                return HarnessRunResult(
                    status="failed",
                    transcript=transcript,
                    gates=gate_results,
                    error=(
                        "Не пройдены гейты готовности: "
                        + ", ".join(f"{g.name} (код {g.exit_code})" for g in failed_gates)
                    ),
                )

            # 5. Сбор результата (стратегия адаптера).
            try:
                harvested = self._harvest(handle, spec)
            except HarvestError as exc:
                return HarnessRunResult(
                    status="failed", transcript=transcript, gates=gate_results, error=str(exc)
                )
            return HarnessRunResult(
                status="completed",
                artifacts=tuple(harvested),
                transcript=transcript,
                gates=gate_results,
            )
        finally:
            # Контейнер всегда ephemeral — сносим в любом случае.
            self._sandbox.destroy(handle)

    # --- хуки адаптера ------------------------------------------------------

    def _prepare(self, handle: SandboxHandle, on_log) -> None:
        """Действия после посева до запуска агента. По умолчанию — нет."""
        return None

    def _build_command(self, spec: HarnessRunSpec) -> list[str]:
        raise NotImplementedError

    def _harvest(
        self, handle: SandboxHandle, spec: HarnessRunSpec
    ) -> Sequence[HarvestedArtifact]:
        raise NotImplementedError

    # --- общие помощники сбора ---------------------------------------------

    def _harvest_by_convention(
        self, handle: SandboxHandle, spec: HarnessRunSpec
    ) -> list[HarvestedArtifact]:
        """Сбор по соглашению: ``/work/.povgen/out/<role>.<fmt>`` на каждую роль.

        Общая стратегия для агентов, которые пишут результат в условленные пути
        (Claude Code, generic command-harness). Бросает :class:`HarvestError`,
        если ожидаемый артефакт не положен.
        """
        harvested: list[HarvestedArtifact] = []
        for expected in spec.expected_artifacts:
            file_path = f"{_OUT_DIR}/{expected.role}.{expected.fmt}"
            files = self._sandbox.get_files(handle, file_path)
            content = files.get(file_path)
            if content is None:
                raise HarvestError(
                    f"Агент не положил артефакт роли '{expected.role}' в {file_path}."
                )
            harvested.append(self._harvest_file_as(expected.role, expected.fmt, content))
        return harvested

    @staticmethod
    def _harvest_file_as(role: str, fmt: str, content: bytes) -> HarvestedArtifact:
        """Один файл → артефакт: json парсим в payload, иначе — файловый бандл."""
        if fmt == "json":
            payload = json.loads(content.decode("utf-8"))
            if not isinstance(payload, dict):
                payload = {"value": payload}
            return HarvestedArtifact(role=role, payload=payload, fmt="json")
        return HarvestedArtifact(role=role, files={f"{role}.{fmt}": content}, fmt=fmt)


def shell(command: str) -> list[str]:
    """Псевдоним shell_argv для адаптеров (sh -lc <command>)."""
    return shell_argv(command)
