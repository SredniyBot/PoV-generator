"""Aider-адаптер (Ф7): git-нативный «редактор» как harness-провайдер.

Aider правит файлы в репозитории и коммитит каждую правку — поэтому самый
чистый сбор результата здесь **diff-harvest**: что изменилось относительно
базовой ревизии, то и есть выход узла (файловый бандл). Это и проверка всей
цепочки сбора на дешёвых моделях (litellm), и git-нативная альтернатива
сбору-по-соглашению (Claude Code).

Тонкая специализация :class:`SandboxHarnessProvider`: отличается командой
запуска, подготовкой (инициализация git + базовая ревизия) и стратегией сбора
(diff против базовой ревизии). Остальное — общая обвязка.

Реальный прогон требует образа с установленным ``aider`` и кредами модели
(эфемерно, в песочнице). В CI проверяется на ``StubSandboxRuntime`` (эмуляция
git+aider через exec_handler) — без Docker и сети.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from ..protocol import HarnessRunSpec, HarvestedArtifact
from ..sandbox import ResourceLimits, SandboxHandle, SandboxRuntime
from .base import _BRIEF_PATH, HarvestError, SandboxHarnessProvider, shell

# Тег базовой ревизии: фиксируем состояние ДО прогона агента, чтобы собрать
# ровно его правки (diff против тега), не таща весь репозиторий.
_BASE_TAG = "povgen-base"

# Инициализация репозитория и фиксация базовой ревизии. Идемпотентно: если
# /work уже git-репозиторий, init ничего не ломает. --allow-empty — на случай
# пустого посева.
_PREPARE_GIT = (
    "cd /work && git init -q && "
    "git config user.email povgen@local && git config user.name povgen && "
    "git add -A && git commit -q -m baseline --allow-empty && "
    f"git tag -f {_BASE_TAG}"
)

# Сбор изменений: стейджим всё (новые/изменённые/удалённые) и берём имена путей,
# изменившихся относительно базовой ревизии.
_HARVEST_DIFF = f"cd /work && git add -A && git diff --cached --name-only {_BASE_TAG}"


class AiderHarnessProvider(SandboxHarnessProvider):
    """Запускает aider по brief и собирает изменённые файлы как бандл (diff)."""

    def __init__(
        self,
        *,
        sandbox: SandboxRuntime,
        image: str,
        model: str | None = None,
        name: str = "aider",
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

    def _prepare(self, handle: SandboxHandle, on_log) -> None:
        # Базовая ревизия для diff-harvest. Сбой здесь не критичен для прогона
        # как такового, но без него сбор не сработает — логируем и продолжаем
        # (харвест ниже даст понятную ошибку, если изменений не видно).
        self._sandbox.exec(handle, shell(_PREPARE_GIT), on_log=on_log)

    def _build_command(self, spec: HarnessRunSpec) -> list[str]:
        # Неинтерактивный прогон по постановке из файла. Модель — опциональна
        # (litellm-имя); образ может задавать её по умолчанию.
        parts = ["aider", "--yes", f"--message-file {shlex.quote(_BRIEF_PATH)}"]
        if self.model:
            parts.append(f"--model {shlex.quote(self.model)}")
        return shell("cd /work && " + " ".join(parts))

    def _harvest(
        self, handle: SandboxHandle, spec: HarnessRunSpec
    ) -> Sequence[HarvestedArtifact]:
        if not spec.expected_artifacts:
            raise HarvestError("Не задан ожидаемый артефакт для diff-harvest.")
        role = spec.expected_artifacts[0].role
        diff = self._sandbox.exec(handle, shell(_HARVEST_DIFF))
        paths = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
        files: dict[str, bytes] = {}
        for path in paths:
            abs_path = f"/work/{path}"
            got = self._sandbox.get_files(handle, abs_path)
            content = got.get(abs_path)
            if content is not None:
                files[path] = content
        if not files:
            raise HarvestError(
                "Агент не внёс изменений в репозиторий — нечего собирать (diff пуст)."
            )
        return [HarvestedArtifact(role=role, files=files, fmt="files")]
