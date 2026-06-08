"""Aider-адаптер (Ф7): git-нативный «редактор» как harness-провайдер.

Aider правит файлы в репозитории и коммитит каждую правку — поэтому ему нужен
git-репозиторий (``_prepare`` инициализирует его). Сбор результата — единый для
всех адаптеров **subtree-harvest** (RG-C): дерево кода из зоны узла
(``harvest_path``) или всего ``/work``. Это согласуется с моделью общего тома и
каркаса (компонент пишет в зону сервиса поверх скелета), и снимает хрупкость
diff-harvest, который в shared-volume давал пустой diff после авто-коммита aider.

Тонкая специализация :class:`SandboxHarnessProvider`: отличается командой запуска
и подготовкой (инициализация git, нужная самому aider). Сбор — общий
``_harvest_by_convention`` (для bundle → subtree зоны).

Реальный прогон требует образа с установленным ``aider`` и кредами модели
(эфемерно, в песочнице). В CI проверяется на ``StubSandboxRuntime`` (эмуляция
git+aider через exec_handler) — без Docker и сети.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from ..protocol import HarnessRunSpec, HarvestedArtifact
from ..sandbox import ResourceLimits, SandboxHandle, SandboxRuntime
from .base import _BRIEF_PATH, SandboxHarnessProvider, shell

# Инициализация git-репозитория для работы aider (он коммитит правки). Базовый
# коммит нужен, чтобы aider стартовал на чистом репозитории. Идемпотентно.
_PREPARE_GIT = (
    "cd /work && git init -q && "
    "git config user.email povgen@local && git config user.name povgen && "
    "git add -A && git commit -q -m baseline --allow-empty"
)


class AiderHarnessProvider(SandboxHarnessProvider):
    """Запускает aider по brief; собирает результат subtree-harvest (как все)."""

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
        # Неинтерактивный прогон по постановке из файла. Модель: явный override
        # подключения ИЛИ настроенная LLM-модель проекта (model_hint) — НЕ
        # выдуманный дефолт. Если обе пусты — образ/litellm берут свою.
        parts = ["aider", "--yes", f"--message-file {shlex.quote(_BRIEF_PATH)}"]
        model = self.model or spec.model_hint
        if model:
            parts.append(f"--model {shlex.quote(model)}")
        return shell("cd /work && " + " ".join(parts))

    def _harvest(
        self, handle: SandboxHandle, spec: HarnessRunSpec
    ) -> Sequence[HarvestedArtifact]:
        # Единый сбор: дерево кода из зоны узла (harvest_path) или всего /work
        # (RG-C). Согласовано с claude_code/command — одна стратегия на все
        # адаптеры; .git служебным каталогом отсеивается как и .povgen.
        return self._harvest_by_convention(handle, spec)
