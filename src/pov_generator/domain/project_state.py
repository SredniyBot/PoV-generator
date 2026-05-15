"""Композитное состояние проекта.

Содержит три концерна, каждый со своим жизненным циклом:

- :class:`ProjectManifest` — иммутабельные метаданные проекта (id, имя,
  objective, исходный запрос, время создания). Заполняется при создании
  проекта и не меняется.
- :class:`ProjectKnowledge` (Layer A) — знание о проекте: положения проекта.
- :class:`ProcessState` (Layer B) — состояние процесса: пробелы, готовность,
  активные паки, режим вовлечённости.

ProjectState — композиция, не наследование, не плоская структура.
У каждого слоя свой жизненный цикл, свои патчи, свой смысл изменений.

См. roadmap, Этап 0.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .process_state import ProcessState
from .project_knowledge import ProjectKnowledge


StateLayer = Literal["knowledge", "process"]
"""Слой, к которому относится событие изменения состояния."""


@dataclass(frozen=True)
class ProjectManifest:
    """Иммутабельные метаданные проекта.

    Заполняются при создании проекта, не меняются за время его жизни.
    Хранятся в `project.json` для быстрого человеческого осмотра без
    обращения к базе данных.
    """

    project_id: str
    name: str
    objective_ref: str
    business_request: str
    created_at: str


@dataclass(frozen=True)
class StateEvent:
    """Событие изменения состояния проекта.

    Унифицированная запись для обоих слоёв — поле ``layer`` различает,
    к какому именно патч относился. Event log один на проект; обходом
    можно восстановить любое состояние на любой момент времени.
    """

    layer: StateLayer
    version: int
    patch_type: str
    payload: dict[str, object]
    actor: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class ProjectState:
    """Композитный снимок состояния проекта.

    Три слоя, каждый со своим жизненным циклом. ``ProjectState`` —
    точка чтения; запись производится через патчи соответствующего
    слоя.
    """

    manifest: ProjectManifest
    knowledge: ProjectKnowledge
    process: ProcessState

    @property
    def snapshot_version(self) -> tuple[int, int]:
        """Полный отпечаток состояния как пара версий слоёв.

        ``(knowledge.version, process.version)``. Один снимок «новее»
        другого, если обе координаты ≥ и хотя бы одна >.
        """
        return (self.knowledge.version, self.process.version)

    def with_knowledge(self, knowledge: ProjectKnowledge) -> "ProjectState":
        """Новый снимок с заменённым слоем знаний."""
        return ProjectState(manifest=self.manifest, knowledge=knowledge, process=self.process)

    def with_process(self, process: ProcessState) -> "ProjectState":
        """Новый снимок с заменённым слоем процесса."""
        return ProjectState(manifest=self.manifest, knowledge=self.knowledge, process=process)
