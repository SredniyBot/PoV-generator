from __future__ import annotations

from pathlib import Path
import os

from pov_generator.common.env import load_env_file


def test_load_env_file_reads_values_without_overriding_existing(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POV_EXECUTION_PROVIDER=openrouter",
                "POV_OPENROUTER_MODEL=\"openai/gpt-4.1-mini\"",
                "POV_OPENROUTER_API_KEY=test-key # comment",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("POV_EXECUTION_PROVIDER", "stub")

    load_env_file(env_file)

    assert os.environ["POV_EXECUTION_PROVIDER"] == "stub"
    assert os.environ["POV_OPENROUTER_MODEL"] == "openai/gpt-4.1-mini"
    assert os.environ["POV_OPENROUTER_API_KEY"] == "test-key"


def test_load_env_file_can_override_existing_value(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POV_EXECUTION_PROVIDER=openrouter", encoding="utf-8")
    monkeypatch.setenv("POV_EXECUTION_PROVIDER", "stub")

    load_env_file(env_file, override=True)

    assert os.environ["POV_EXECUTION_PROVIDER"] == "openrouter"
