"""Тесты для серверного рендера Mermaid через ``mmdc``.

Покрытие:
* успех subprocess → возвращаются PNG-байты + кеш по SHA-256;
* non-zero exit / отсутствие выходного файла → None;
* отсутствие бинаря (FileNotFoundError) → None;
* таймаут → None;
* ``POV_MERMAID_DISABLED`` коротко замыкает на None без вызова subprocess;
* пустой/невалидный исходник → None без вызова subprocess.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pov_generator.application import mermaid_render


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбросить кеш + удалить env-переключатель отключения."""
    mermaid_render.clear_cache()
    monkeypatch.delenv("POV_MERMAID_DISABLED", raising=False)
    monkeypatch.delenv("POV_MERMAID_CLI", raising=False)
    monkeypatch.delenv("POV_MERMAID_TIMEOUT", raising=False)
    # Бинарь «находится» как есть: эти тесты мокают сам subprocess.run, реальный
    # mmdc им не нужен и не должен влиять. Локальную установку проекта отключаем
    # (resolve через локальный путь / which проверяется ниже точечно).
    monkeypatch.setattr(mermaid_render, "_local_bin_candidates", lambda: [])
    monkeypatch.setattr(mermaid_render.shutil, "which", lambda name, *a, **k: name)


def _make_fake_run(
    *,
    returncode: int = 0,
    png_bytes: bytes | None = b"\x89PNG\r\n\x1a\nfake-content",
    stderr: bytes = b"",
    raises: Exception | None = None,
):
    """Возвращает функцию, которую можно подсунуть как ``subprocess.run``.

    Создаёт «выходной» PNG-файл, если ``png_bytes`` задано и ``returncode``
    равен 0 — имитирует поведение настоящего mmdc, который пишет файл,
    указанный в ``-o``.
    """

    def fake_run(cmd, *args, **kwargs):
        if raises is not None:
            raise raises
        # Найти "-o /path/to/diagram.png" в cmd и записать туда байты.
        if returncode == 0 and png_bytes is not None:
            for i, token in enumerate(cmd):
                if token == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_bytes(png_bytes)
                    break
        return SimpleNamespace(returncode=returncode, stdout=b"", stderr=stderr)

    return fake_run


def test_render_returns_png_bytes_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = b"\x89PNG\r\n\x1a\nhello"
    monkeypatch.setattr(
        subprocess, "run", _make_fake_run(returncode=0, png_bytes=expected)
    )
    out = mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B")
    assert out == expected


def test_render_caches_by_source_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def counting_run(cmd, *args, **kwargs):
        calls["count"] += 1
        for i, token in enumerate(cmd):
            if token == "-o" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_bytes(b"PNG-OK")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", counting_run)
    mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B")
    mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B")
    assert calls["count"] == 1


def test_render_treats_different_sources_as_distinct_cache_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def counting_run(cmd, *args, **kwargs):
        calls["count"] += 1
        for i, token in enumerate(cmd):
            if token == "-o" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_bytes(b"PNG")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", counting_run)
    mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B")
    mermaid_render.render_mermaid_to_png("sequenceDiagram\nA->>B: ping")
    assert calls["count"] == 2


def test_render_returns_none_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _make_fake_run(returncode=1, png_bytes=None, stderr=b"boom"),
    )
    assert mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B") is None


def test_render_returns_none_when_output_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """returncode == 0, но файл не создан — рендер всё равно неудачен."""

    def fake_run(cmd, *args, **kwargs):
        # Намеренно НЕ пишем PNG-файл.
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B") is None


def test_render_returns_none_when_binary_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _make_fake_run(raises=FileNotFoundError("no mmdc")),
    )
    assert mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B") is None


def test_render_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _make_fake_run(raises=subprocess.TimeoutExpired(cmd="mmdc", timeout=1)),
    )
    assert mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B") is None


def test_render_short_circuits_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POV_MERMAID_DISABLED", "1")
    # subprocess.run не должен вызываться вообще.
    sentinel = SimpleNamespace(called=False)

    def fake_run(cmd, *args, **kwargs):
        sentinel.called = True
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B") is None
    assert sentinel.called is False


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_render_returns_none_for_empty_input(
    bad, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = SimpleNamespace(called=False)

    def fake_run(cmd, *args, **kwargs):
        sentinel.called = True
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mermaid_render.render_mermaid_to_png(bad) is None
    assert sentinel.called is False


def test_render_uses_custom_binary_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        for i, token in enumerate(cmd):
            if token == "-o" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_bytes(b"PNG")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setenv("POV_MERMAID_CLI", "/opt/custom/mmdc")
    monkeypatch.setattr(subprocess, "run", fake_run)
    out = mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B")
    assert out == b"PNG"
    assert captured["cmd"][0] == "/opt/custom/mmdc"


def test_render_respects_custom_timeout_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, int] = {}

    def fake_run(cmd, *args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        for i, token in enumerate(cmd):
            if token == "-o" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_bytes(b"PNG")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setenv("POV_MERMAID_TIMEOUT", "5")
    monkeypatch.setattr(subprocess, "run", fake_run)
    mermaid_render.render_mermaid_to_png("flowchart LR\nA --> B")
    assert captured["timeout"] == 5


def test_resolve_binary_uses_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без локальной установки и override _resolve_binary падает в PATH-фоллбек
    shutil.which (учитывает PATHEXT на Windows → mmdc.cmd). None → None."""
    monkeypatch.setattr(mermaid_render.shutil, "which", lambda name, *a, **k: r"C:\npm\mmdc.cmd")
    assert mermaid_render._resolve_binary() == r"C:\npm\mmdc.cmd"
    monkeypatch.setattr(mermaid_render.shutil, "which", lambda name, *a, **k: None)
    assert mermaid_render._resolve_binary() is None


def test_resolve_binary_prefers_local_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Локальная установка (ui/workspace/node_modules/.bin) приоритетнее PATH —
    значит работает на новой среде после npm ci, без глобального mmdc."""
    local = tmp_path / "mmdc.cmd"
    local.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(mermaid_render, "_local_bin_candidates", lambda: [local])
    # which вернул бы глобальный, но он не должен использоваться.
    monkeypatch.setattr(
        mermaid_render.shutil, "which", lambda name, *a, **k: r"C:\global\mmdc.cmd"
    )
    assert mermaid_render._resolve_binary() == str(local)


def test_resolve_binary_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """POV_MERMAID_CLI имеет наивысший приоритет (override локального и PATH)."""
    monkeypatch.setenv("POV_MERMAID_CLI", "/opt/custom/mmdc")
    monkeypatch.setattr(mermaid_render, "_local_bin_candidates", lambda: [Path("/should/not/use")])
    monkeypatch.setattr(mermaid_render.shutil, "which", lambda name, *a, **k: name)
    assert mermaid_render._resolve_binary() == "/opt/custom/mmdc"


def test_build_command_wraps_cmd_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """На Windows .cmd/.bat (npm-обёртка mmdc.cmd) заворачивается в `cmd /c` —
    иначе subprocess(shell=False) его не исполнит. .exe и POSIX — как есть."""
    monkeypatch.setattr(mermaid_render.sys, "platform", "win32")
    assert mermaid_render._build_command(r"C:\npm\mmdc.cmd", ["-i", "a"]) == [
        "cmd", "/c", r"C:\npm\mmdc.cmd", "-i", "a",
    ]
    assert mermaid_render._build_command(r"C:\npm\mmdc.exe", ["-i", "a"]) == [
        r"C:\npm\mmdc.exe", "-i", "a",
    ]
    monkeypatch.setattr(mermaid_render.sys, "platform", "linux")
    assert mermaid_render._build_command("/usr/bin/mmdc", ["-i", "a"]) == [
        "/usr/bin/mmdc", "-i", "a",
    ]


# --- SVG-путь (вектор для PDF) ----------------------------------------------


def _make_fake_run_svg(svg_text: str, *, returncode: int = 0):
    """Фейковый ``subprocess.run``, пишущий SVG в путь из ``-o``."""

    def fake_run(cmd, *args, **kwargs):
        if returncode == 0:
            for i, token in enumerate(cmd):
                if token == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text(svg_text, encoding="utf-8")
                    break
        return SimpleNamespace(returncode=returncode, stdout=b"", stderr=b"")

    return fake_run


# Минимальный валидный SVG с НУЛЕВЫМ dash (его reportlab не переваривает —
# поэтому санитизация обязательна) и настоящим <text> (без foreignObject).
_OK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
    '<rect x="1" y="1" width="38" height="18" fill="none" stroke="black" '
    'stroke-dasharray="0"/>'
    '<text x="5" y="14">Ok</text></svg>'
)


def test_sanitize_svg_removes_zero_dash_keeps_real_dash() -> None:
    assert "stroke-dasharray" not in mermaid_render._sanitize_svg('a stroke-dasharray="0" b')
    assert "stroke-dasharray" not in mermaid_render._sanitize_svg("a stroke-dasharray:0; b")
    # реальный пунктир (есть ненулевая цифра) — сохраняем
    kept = mermaid_render._sanitize_svg('a stroke-dasharray="3, 3" b')
    assert 'stroke-dasharray="3, 3"' in kept


def test_render_svg_success_sanitizes_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _make_fake_run_svg(_OK_SVG))
    out = mermaid_render.render_mermaid_to_svg("flowchart LR\nA --> B")
    assert out is not None
    # нулевой dash убран (иначе svglib/reportlab упал бы), <text> на месте
    assert "stroke-dasharray" not in out
    assert "<text" in out


def test_render_svg_none_when_not_convertible(monkeypatch: pytest.MonkeyPatch) -> None:
    # Невалидный/непарсимый SVG → svglib не сконвертирует → None (fallback на PNG).
    monkeypatch.setattr(subprocess, "run", _make_fake_run_svg("не svg вовсе"))
    assert mermaid_render.render_mermaid_to_svg("flowchart LR\nA --> B") is None


def test_render_svg_disabled_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POV_MERMAID_DISABLED", "1")

    def boom(*a, **k):  # subprocess не должен вызываться
        raise AssertionError("subprocess.run не должен вызываться при DISABLED")

    monkeypatch.setattr(subprocess, "run", boom)
    assert mermaid_render.render_mermaid_to_svg("flowchart LR\nA --> B") is None


def test_render_svg_caches_by_source(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def counting(cmd, *args, **kwargs):
        calls["n"] += 1
        for i, token in enumerate(cmd):
            if token == "-o" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_text(_OK_SVG, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", counting)
    mermaid_render.render_mermaid_to_svg("flowchart LR\nA --> B")
    mermaid_render.render_mermaid_to_svg("flowchart LR\nA --> B")
    assert calls["n"] == 1
