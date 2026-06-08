"""#3: профессиональный UI/UX дизайн-шаг гейта «Архитектура».

frontend-пакет вкладывает шаг ui_design в slot architecture.domain_views —
он производит структурный дизайн (палитра/типографика/экраны/формы/компоненты +
подход к API), рендерится в раздел и органично входит в design_document.
"""

from __future__ import annotations

import json
from pathlib import Path

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    render_markdown,
    validate_json_schema,
)
from pov_generator.application.registry_service import RegistryService
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "templates" / "stub_fixtures"


def test_ui_design_schema_and_render() -> None:
    payload = json.loads((FIXTURES / "ui_design.json").read_text(encoding="utf-8"))
    validate_json_schema(payload, artifact_schema("ui_design"))

    md = render_markdown("ui_design", payload)
    assert "# UI/UX дизайн" in md
    assert "Цветовая палитра" in md and "#2D6CDF" in md  # палитра как таблица
    assert "Экраны" in md and "Список заявок" in md
    assert "Взаимодействие с API" in md and "GET /requests" in md


def test_frontend_pack_contributes_ui_design_to_architecture_slot() -> None:
    snapshot, report = RegistryService(
        FilesystemRegistryLoader(REPO_ROOT / "templates")
    ).validate()
    assert report.is_valid

    # Архитектурный композит объявляет slot domain_views.
    arch = snapshot.resolve_template("architecture.prepare_architecture_doc@1.0.0")
    assert any(s.identifier == "architecture.domain_views" for s in arch.slots)

    # frontend-пакет вкладывает ui_design в этот slot.
    pack = snapshot.resolve_domain_pack("frontend.web_workspace@1.0.0")
    contrib = next(
        (c for c in pack.contributions if c.slot_id == "architecture.domain_views"), None
    )
    assert contrib is not None
    refs = {i.task_ref.as_string() for i in contrib.items if i.task_ref}
    assert "frontend.ui_design@1.0.0" in refs

    # Шаг ui_design — leaf, llm, производит роль ui_design.
    task = snapshot.resolve_template("frontend.ui_design@1.0.0")
    assert task.template_type == "leaf"
    assert task.executor == "llm"
    assert task.outputs.artifact_roles == ("ui_design",)
