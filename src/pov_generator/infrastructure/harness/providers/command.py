"""Generic command-harness: запуск произвольного агент-CLI в песочнице (Ф2).

Это и escape hatch (интеграция нестандартного агента), и первая проверка связки
песочница↔контракт harness. Тонкая специализация общей обвязки
(:class:`SandboxHarnessProvider`): задаёт команду и собирает результат по
соглашению (``/work/.povgen/out/<role>.<fmt>``).

Движок-агностичен: c ``StubSandboxRuntime`` тестируется в CI без Docker, c
``DockerSandboxRuntime`` — реальный прогон. Конкретные адаптеры (Claude Code,
Aider) — другие специализации той же базы (Ф7).
"""

from __future__ import annotations

from collections.abc import Sequence

from ..protocol import HarnessRunSpec, HarvestedArtifact
from ..sandbox import ResourceLimits, SandboxHandle, SandboxRuntime, shell_argv
from .base import _OUT_DIR, HarvestError, SandboxHarnessProvider


class CommandHarnessProvider(SandboxHarnessProvider):
    """Запускает заданную команду и собирает артефакты по соглашению."""

    def __init__(
        self,
        *,
        sandbox: SandboxRuntime,
        image: str,
        command: str | Sequence[str],
        name: str = "command",
        model: str | None = None,
        resource_limits: ResourceLimits | None = None,
        default_timeout_s: int | None = None,
    ) -> None:
        super().__init__(
            sandbox=sandbox,
            image=image,
            name=name,
            model=model,
            resource_limits=resource_limits,
            default_timeout_s=default_timeout_s,
        )
        self._command = command

    def _build_command(self, spec: HarnessRunSpec) -> list[str]:
        if isinstance(self._command, str):
            return shell_argv(self._command)
        return list(self._command)

    def _harvest(
        self, handle: SandboxHandle, spec: HarnessRunSpec
    ) -> Sequence[HarvestedArtifact]:
        """Сбор по соглашению: ``/work/.povgen/out/<role>.<fmt>``."""
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
