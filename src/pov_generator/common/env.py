from __future__ import annotations

from pathlib import Path
import os
import re


_ENV_LINE_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def load_repo_env(repo_root: Path, *, override: bool = False) -> None:
    load_env_file(repo_root / ".env", override=override)


def load_env_file(path: Path, *, override: bool = False) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        match = _ENV_LINE_PATTERN.match(raw_line)
        if not match:
            continue

        key = match.group(1)
        value = _normalize_env_value(match.group(2))
        if not override and key in os.environ:
            continue
        os.environ[key] = value


def _normalize_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    if value[0] == value[-1] and value[0] in {"'", '"'}:
        quote = value[0]
        inner = value[1:-1]
        if quote == '"':
            inner = (
                inner.replace(r"\\", "\\")
                .replace(r"\n", "\n")
                .replace(r"\r", "\r")
                .replace(r"\t", "\t")
                .replace(r"\"", '"')
            )
        return inner

    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value
