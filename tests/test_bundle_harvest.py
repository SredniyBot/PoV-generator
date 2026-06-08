"""RG-C: реальный сбор файлового бандла из песочницы.

Узел-агент пишет код в рабочий каталог; harvest собирает дерево из зоны сервиса
(``harvest_path``) или всего ``/work``, исключая служебный ``.povgen`` и приводя
пути к относительным. Проверяется на StubSandboxRuntime через generic-адаптер.
"""

from __future__ import annotations

from pov_generator.application.harness_execution_service import HarnessExecutionService
from pov_generator.infrastructure.harness import (
    HarnessConnection,
    HarnessProviderRegistry,
    StubSandboxRuntime,
)
from pov_generator.infrastructure.harness.sandbox import ExecResult


def _service_writing(files: dict[str, bytes]) -> HarnessExecutionService:
    def handler(rt, handle, argv):  # noqa: ANN001 — тестовый агент
        rt.put_files(handle, files)
        return ExecResult(exit_code=0, stdout="", stderr="")

    registry = HarnessProviderRegistry(
        connection=HarnessConnection(provider="command", command="build", image="x"),
        sandbox=StubSandboxRuntime(exec_handler=handler),
    )
    return HarnessExecutionService(registry)


def test_bundle_harvest_scopes_to_service_zone() -> None:
    service = _service_writing(
        {
            "/work/services/backend/src/main.py": b"print('hi')",
            "/work/services/backend/README.md": b"# backend",
            "/work/services/web/app.tsx": b"x",  # другой сервис — вне зоны
            "/work/.povgen/notes.txt": b"internal",  # исключить
        }
    )
    outcome = service.produce_artifact(
        artifact_role="component_implementation",
        system_prompt="S",
        user_prompt="U",
        output_kind="bundle",
        harvest_path="/work/services/backend",
    )
    assert outcome.files is not None
    assert set(outcome.files) == {"src/main.py", "README.md"}
    assert outcome.files["src/main.py"] == b"print('hi')"


def test_bundle_harvest_whole_work_excludes_povgen() -> None:
    service = _service_writing(
        {
            "/work/services/backend/src/main.py": b"a",
            "/work/docker-compose.yml": b"services:",
            "/work/.povgen/out/x.json": b"{}",  # служебное — исключить
        }
    )
    outcome = service.produce_artifact(
        artifact_role="project_scaffold",
        system_prompt="S",
        user_prompt="U",
        output_kind="bundle",
    )
    assert outcome.files is not None
    assert set(outcome.files) == {"services/backend/src/main.py", "docker-compose.yml"}


def test_bundle_harvest_excludes_inputs_and_agent_files() -> None:
    # В бандл идёт только код: посеянные реквизиты, рабочие файлы agent/git/кэш
    # исключаются (иначе бандл забивается мусором — #2).
    service = _service_writing(
        {
            "/work/src/main.py": b"code",
            "/work/.aider.chat.history.md": b"chat",
            "/work/.aider.tags.cache.v3": b"cache",
            "/work/.git/config": b"[core]",
            "/work/__pycache__/x.pyc": b"\x00",
            "/work/requisite.txt": b"seeded material",
        }
    )
    outcome = service.produce_artifact(
        artifact_role="component_implementation",
        system_prompt="S",
        user_prompt="U",
        output_kind="bundle",
        inputs={"requisite.txt": "seeded material"},
    )
    assert outcome.files is not None
    assert set(outcome.files) == {"src/main.py"}


def test_bundle_harvest_empty_zone_fails_loudly() -> None:
    from pov_generator.common.errors import ConflictError

    service = _service_writing({"/work/other/file.py": b"x"})
    try:
        service.produce_artifact(
            artifact_role="component_implementation",
            system_prompt="S",
            user_prompt="U",
            output_kind="bundle",
            harvest_path="/work/services/empty",
        )
    except ConflictError:
        return
    raise AssertionError("Ожидался ConflictError при пустой зоне сбора бандла.")
