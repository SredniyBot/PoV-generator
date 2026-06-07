"""Файловый артефакт-бандл: общая модель для разнородных выходов (Ф5).

Узел может произвести не только структурный JSON, но и код, документы, двоичные
объекты, БД, архивы, Docker-образ. Всё это — «бандл»: набор файлов + лёгкий
манифест (путь, размер, sha256, вид содержимого) и общий вид бандла. Файлы лежат
на диске; в БД — только манифест (SQLite блобами не раздуваем).

Модель и классификация — чистые (без I/O), чтобы тестировать в изоляции.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

# Вид содержимого ОТДЕЛЬНОГО файла — для рендера/иконки/способа просмотра.
ContentKind = Literal[
    "code",
    "document",
    "text",
    "data",
    "binary",
    "database",
    "archive",
    "container_image",
    "other",
]


@dataclass(frozen=True)
class BundleFile:
    """Один файл бандла (метаданные; содержимое — на диске)."""

    path: str  # относительный путь внутри бандла (posix)
    size_bytes: int
    sha256: str
    content_kind: ContentKind


@dataclass(frozen=True)
class BundleManifest:
    """Манифест бандла — то, что лежит в БД вместо самих файлов."""

    bundle_kind: str  # доминирующий/объявленный вид: code|documents|database|container_image|mixed|files|empty
    total_files: int
    total_bytes: int
    files: tuple[BundleFile, ...]
    entry_point: str | None = None


# --- классификация по расширению ------------------------------------------------

_EXT_KIND: dict[str, ContentKind] = {}


def _register(kind: ContentKind, *exts: str) -> None:
    for ext in exts:
        _EXT_KIND[ext] = kind


_register(
    "code",
    "py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "rb", "php", "c", "h",
    "cpp", "cc", "hpp", "cs", "kt", "kts", "swift", "scala", "sh", "bash", "ps1",
    "lua", "r", "m", "mm", "dart", "ex", "exs", "clj", "vue", "svelte",
)
_register("data", "json", "yaml", "yml", "toml", "ini", "env", "csv", "tsv", "xml", "proto")
_register("document", "md", "rst", "adoc", "pdf", "docx", "doc", "odt", "rtf", "pptx", "xlsx", "html", "htm")
_register("text", "txt", "log", "license", "gitignore", "dockerignore")
_register("database", "db", "sqlite", "sqlite3", "sql", "mdb", "dump", "bak")
_register("archive", "zip", "tar", "gz", "tgz", "bz2", "xz", "zst", "7z", "rar")
_register("binary", "exe", "dll", "so", "dylib", "bin", "o", "a", "lib", "wasm",
          "class", "jar", "pyc", "node", "whl", "png", "jpg", "jpeg", "gif",
          "ico", "webp", "bmp", "ttf", "otf", "woff", "woff2", "onnx", "pt", "pth", "pb", "h5")

# Файлы без расширения, узнаваемые по имени.
_NAME_KIND: dict[str, ContentKind] = {
    "dockerfile": "code",
    "makefile": "code",
    "readme": "document",
    "license": "document",
}

# Магические сигнатуры (когда расширение не помогло).
_MAGIC: tuple[tuple[bytes, ContentKind], ...] = (
    (b"SQLite format 3\x00", "database"),
    (b"PK\x03\x04", "archive"),
    (b"\x1f\x8b", "archive"),       # gzip
    (b"%PDF", "document"),
    (b"\x7fELF", "binary"),
    (b"MZ", "binary"),              # PE (exe/dll)
)


def classify_content(path: str, data: bytes | None = None) -> ContentKind:
    """Определить вид содержимого по пути и (опц.) первым байтам.

    Сначала по расширению/имени; если неизвестно — по магической сигнатуре;
    иначе эвристика «есть NUL-байты → binary, иначе text».
    """
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    stem = name.split(".", 1)[0]
    ext = name.rsplit(".", 1)[-1] if "." in name else ""

    if ext and ext in _EXT_KIND:
        return _EXT_KIND[ext]
    if name in _NAME_KIND:
        return _NAME_KIND[name]
    if stem in _NAME_KIND:
        return _NAME_KIND[stem]

    if data:
        for signature, kind in _MAGIC:
            if data.startswith(signature):
                return kind
        if b"\x00" in data[:8192]:
            return "binary"
        return "text"
    return "other"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _derive_bundle_kind(kinds: list[ContentKind]) -> str:
    if not kinds:
        return "empty"
    if "container_image" in kinds:
        return "container_image"
    distinct = set(kinds)
    if len(distinct) == 1:
        only = next(iter(distinct))
        return {
            "code": "code",
            "document": "documents",
            "text": "documents",
            "data": "data",
            "binary": "binaries",
            "database": "database",
            "archive": "archives",
            "other": "files",
        }.get(only, "files")
    return "mixed"


def build_manifest(
    files: Mapping[str, bytes],
    *,
    bundle_kind: str | None = None,
    entry_point: str | None = None,
    kind_overrides: Mapping[str, ContentKind] | None = None,
) -> BundleManifest:
    """Собрать манифест по содержимому файлов (чистая функция).

    ``kind_overrides`` — явный вид для отдельных путей (например, образ-tarball
    помечается ``container_image`` производителем). ``bundle_kind`` — явный
    общий вид; иначе выводится из видов файлов.
    """
    overrides = dict(kind_overrides or {})
    entries: list[BundleFile] = []
    for path in sorted(files):
        data = files[path]
        kind = overrides.get(path) or classify_content(path, data)
        entries.append(
            BundleFile(path=path, size_bytes=len(data), sha256=_sha256(data), content_kind=kind)
        )
    total_bytes = sum(entry.size_bytes for entry in entries)
    resolved_kind = bundle_kind or _derive_bundle_kind([entry.content_kind for entry in entries])
    return BundleManifest(
        bundle_kind=resolved_kind,
        total_files=len(entries),
        total_bytes=total_bytes,
        files=tuple(entries),
        entry_point=entry_point,
    )
