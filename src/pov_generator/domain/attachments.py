"""Входные файлы-вложения проекта.

Вложение (``AttachmentRecord``) — отдельная сущность, НЕ артефакт: артефакты
неизменяемы, версионируются и подчинены JSON-контракту под каждую
``artifact_role``; загруженный файл контракту не подчиняется и может быть
бинарным. Поэтому вложения живут в собственной таблице + файле на диске и лишь
проецируются в «окно артефактов» отдельной вкладкой.

Текст успешно извлечённого вложения подаётся задачам в контекст штатным
механизмом слоя A (Position ``source="input"``), как и бизнес-запрос проекта.

Послабление неизменяемости: вложение можно удалить, пока ни одна задача не
использовала его текст в контексте (``used_in_context = False``). Как только
текст вошёл в контекст исполнения — удаление запрещено (иначе теряется
воспроизводимость уже созданных артефактов).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExtractionStatus = Literal["pending", "succeeded", "failed", "unsupported"]
"""Статус извлечения текста из вложения.

- ``pending`` — файл сохранён, извлечение поставлено в фон, ещё не завершено;
- ``succeeded`` — текст извлечён и подан в контекст (Layer A);
- ``failed`` — извлечение упало (повреждение, шифрование, пустой результат);
- ``unsupported`` — формат не поддержан для извлечения текста.

При ``failed``/``unsupported`` файл всё равно хранится и скачивается, но в
контекст НЕ попадает (graceful degradation).
"""


@dataclass(frozen=True)
class AttachmentRecord:
    """Загруженный входной файл проекта.

    Бинарь/исходник лежит на диске по ``storage_path`` (под сгенерированным
    ``attachment_id``, не под пользовательским именем — гигиена против path
    traversal). Извлечённый текст — рядом, по ``extracted_text_ref``.
    """

    attachment_id: str
    project_id: str
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_path: str
    extraction_status: ExtractionStatus
    created_at: str
    extracted_text_ref: str | None = None
    extraction_error: str | None = None
    # Position слоя A, в которую внесён извлечённый текст (если успешно).
    linked_position_id: str | None = None
    # Текст вошёл в контекст хотя бы одной задачи — запрещает удаление.
    used_in_context: bool = False
    # Мягкое удаление (до первого использования).
    is_deleted: bool = False
    # Назначение вложения (реквизиты v2): "input" — входной материал проекта
    # (push, показывается во «Входных материалах»); "requisite" — файл,
    # предоставленный в ответ на конкретный реквизит (pull, в «Реквизитах»).
    # Разделяет два идеологически разных бакета, не смешивая их в UI.
    purpose: str = "input"

    @property
    def can_delete(self) -> bool:
        """Удаление разрешено только до первого использования в контексте."""
        return not self.is_deleted and not self.used_in_context
