"""Ф4 онбординга harness: проба Docker, подготовка образа, самопроверка, API.

Без Docker. Реальная проба в CI вернёт «недоступен» (SDK не в [dev]); счастливый
путь самопроверки проверяется на stub-песочнице + фейковой пробе.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pov_generator.application.harness_onboarding_service import (
    DEFAULT_SELF_TEST_IMAGE,
    HarnessOnboardingService,
)
from pov_generator.infrastructure.harness.docker_env import DockerStatus, probe_docker
from pov_generator.infrastructure.harness.images import StubImagePreparer
from pov_generator.infrastructure.harness.sandbox import (
    ExecResult,
    SandboxHandle,
    StubSandboxRuntime,
)
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- проба Docker ------------------------------------------------------------


class _FakeClient:
    def ping(self) -> bool:
        return True

    def version(self) -> dict[str, str]:
        return {"Version": "27.1.0"}


def test_probe_docker_available_with_fake_client() -> None:
    status = probe_docker(client_factory=lambda: _FakeClient())
    assert status.available is True
    assert status.version == "27.1.0"


def test_probe_docker_unavailable_when_daemon_down() -> None:
    def boom() -> object:
        raise RuntimeError("Cannot connect to the Docker daemon")

    status = probe_docker(client_factory=boom)
    assert status.available is False
    assert status.error is not None
    assert status.hint is not None


def test_probe_docker_real_without_sdk_degrades() -> None:
    # В CI/[dev] docker SDK не установлен → проба мягко возвращает «недоступен».
    status = probe_docker()
    assert isinstance(status, DockerStatus)
    if not status.available:
        assert status.hint is not None


def test_probe_docker_cli_fallback_detects_daemon(monkeypatch) -> None:
    """Без SDK, но с работающим Docker Desktop: детект через CLI → available."""
    import types

    import pov_generator.infrastructure.harness.docker_env as de

    def no_sdk() -> object:
        raise ImportError("No module named 'docker'")

    monkeypatch.setattr(de.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        de.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="27.5.1\n", stderr=""),
    )
    status = de.probe_docker(client_factory=no_sdk)
    assert status.available is True
    assert status.version == "27.5.1"
    assert status.sdk_installed is False
    assert status.hint is not None  # подсказка про pip install '.[harness]'


def test_probe_docker_cli_fallback_no_cli(monkeypatch) -> None:
    """Без SDK и без docker CLI на PATH → недоступен, sdk_installed=False."""
    import pov_generator.infrastructure.harness.docker_env as de

    def no_sdk() -> object:
        raise ImportError("No module named 'docker'")

    monkeypatch.setattr(de.shutil, "which", lambda name: None)
    status = de.probe_docker(client_factory=no_sdk)
    assert status.available is False
    assert status.sdk_installed is False


# --- подготовка образа -------------------------------------------------------


def test_docker_image_preparer_builds_bundled_image() -> None:
    """Образ агента встроен в проект: prepare СОБИРАЕТ его из bundled Dockerfile
    (а не тянет из registry). Прочие образы (busybox) — обычный pull."""
    from pov_generator.infrastructure.harness.images import (
        _BUNDLED_IMAGES,
        _DOCKERFILES_DIR,
        DockerImagePreparer,
    )

    # Dockerfile'ы реально лежат пакетными данными.
    assert (_DOCKERFILES_DIR / "aider.Dockerfile").exists()
    assert (_DOCKERFILES_DIR / "claude-code.Dockerfile").exists()
    assert "povgen/aider:latest" in _BUNDLED_IMAGES

    calls: dict[str, list] = {"build": [], "pull": []}

    class _Api:
        def build(self, **kw):
            calls["build"].append(kw)
            return iter([{"stream": "Step 1/3"}, {"stream": "Successfully built"}])

        def pull(self, image, **kw):
            calls["pull"].append(image)
            return iter([{"status": "pulling"}])

    class _Images:
        def get(self, image):  # noqa: ANN001 — фейк
            return object()  # образ «готов»

    class _Client:
        api = _Api()
        images = _Images()

    preparer = DockerImagePreparer(client=_Client())
    status = preparer.prepare("povgen/aider:latest")
    assert status.ready is True
    assert len(calls["build"]) == 1
    assert calls["build"][0]["tag"] == "povgen/aider:latest"
    assert calls["build"][0]["dockerfile"] == "aider.Dockerfile"
    assert calls["pull"] == []  # встроенный образ собираем, не тянем

    preparer.prepare("busybox:latest")
    assert calls["pull"] == ["busybox:latest"]  # не встроенный → pull


def test_stub_image_preparer_progress_and_ready() -> None:
    preparer = StubImagePreparer()
    assert preparer.is_ready("x:latest") is False
    seen: list[dict] = []
    result = preparer.prepare("x:latest", on_progress=lambda line: seen.append(dict(line)))
    assert result.ready is True
    assert preparer.is_ready("x:latest") is True
    assert any(line.get("status") == "ready" for line in seen)


# --- сервис онбординга -------------------------------------------------------


def _selftest_agent(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
    rt.put_files(handle, {"/work/.povgen/out/selftest.json": b'{"ok": true}'})
    return ExecResult(exit_code=0, stdout="selftest ok", stderr="")


def _service(*, docker_available: bool, image_ready: bool, agent=_selftest_agent):
    probe = (
        DockerStatus(available=True, version="27.1.0")
        if docker_available
        else DockerStatus(available=False, error="no docker", hint="установите")
    )
    ready_images = {DEFAULT_SELF_TEST_IMAGE} if image_ready else set()
    return HarnessOnboardingService(
        StubSandboxRuntime(exec_handler=agent),
        StubImagePreparer(ready_images=ready_images),
        docker_probe=lambda: probe,
    )


def test_readiness_blocked_without_docker() -> None:
    readiness = _service(docker_available=False, image_ready=False).readiness()
    assert readiness.ready is False
    assert "Docker недоступен" in readiness.blockers
    assert readiness.capacity.max_concurrent >= 1


def test_readiness_ready_when_docker_and_image() -> None:
    readiness = _service(docker_available=True, image_ready=True).readiness()
    assert readiness.ready is True
    assert readiness.blockers == ()
    assert readiness.image_ready is True


def test_readiness_blocked_when_image_missing() -> None:
    readiness = _service(docker_available=True, image_ready=False).readiness()
    assert readiness.ready is False
    assert "Образ агента не подготовлен" in readiness.blockers


def test_start_prepare_marks_image_ready() -> None:
    service = _service(docker_available=True, image_ready=False)
    thread = service.start_prepare()
    thread.join(2.0)
    progress = service.pull_progress()
    assert progress is not None
    assert progress.ready is True
    assert progress.in_progress is False


def test_self_test_ok_on_stub_chain() -> None:
    result = _service(docker_available=True, image_ready=True).self_test()
    assert result.ok is True
    assert result.duration_ms >= 0
    assert "selftest" in result.transcript.lower() or result.transcript == ""


def test_self_test_fails_when_agent_writes_nothing() -> None:
    def silent(rt, handle, argv):  # noqa: ANN001 - тестовый агент-пустышка
        return ExecResult(exit_code=0, stdout="", stderr="")

    result = _service(docker_available=True, image_ready=True, agent=silent).self_test()
    assert result.ok is False
    assert result.error is not None


# --- API ---------------------------------------------------------------------


def test_api_harness_status_degrades_without_docker(tmp_path: Path) -> None:
    app = create_app(repo_root=REPO_ROOT, runtime_root=tmp_path / "runtime")
    client = TestClient(app)
    resp = client.get("/api/harness/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "docker" in body and "capacity" in body
    assert body["capacity"]["max_concurrent"] >= 1
    # Эндпоинт отвечает штатно независимо от наличия Docker (готовность —
    # bool: False без Docker/образа, True при поднятой цепочке).
    assert isinstance(body["ready"], bool)
    assert isinstance(body["blockers"], list)


def test_api_harness_self_test_and_prepare_respond(tmp_path: Path) -> None:
    app = create_app(repo_root=REPO_ROOT, runtime_root=tmp_path / "runtime")
    client = TestClient(app)

    prepared = client.post("/api/harness/prepare", json={})
    assert prepared.status_code == 200
    assert prepared.json()["status"] == "accepted"

    tested = client.post("/api/harness/self-test", json={})
    assert tested.status_code == 200
    # Эндпоинт отвечает штатным результатом независимо от наличия Docker:
    # ok=False без демона, ok=True при поднятой цепочке.
    body = tested.json()
    assert isinstance(body["ok"], bool)
    assert body["duration_ms"] >= 0
