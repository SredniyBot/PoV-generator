"""Симметричное шифрование секретов (API-ключей) при хранении в БД.

Используется ``cryptography.fernet`` — стандартный pure-Python AES-CBC +
HMAC-SHA256 в одном пакете с base64-обёрткой.

Ключ шифрования резолвится в порядке:

1. ``POV_SECRET_KEY`` env var (base64-URL-safe 32 байта). Подходит для
   docker / CI: ключ хранится в инфраструктуре деплоя, не в файле.
2. Файл ``<runtime_root>/.secret_key`` — если env не задан. Генерируется
   автоматически при первом запуске, права 0600. Подходит для локальной
   разработки.

Если оба источника не дали ключа — поднимаем ``ConflictError``. Никакого
implicit plaintext — лучше явная ошибка, чем тихо незашифрованные секреты.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..common.errors import ConflictError

_KEY_ENV_VAR = "POV_SECRET_KEY"
_KEY_FILENAME = ".secret_key"


class SecretBox:
    """Шифровальщик API-ключей.

    Один экземпляр на процесс. Лениво поднимает Fernet с резолвом ключа.

    Args:
        runtime_root: директория для persistent-ключа (если env не задан).
        env_var: переопределить имя env-переменной (для тестов).
    """

    def __init__(
        self,
        runtime_root: Path,
        *,
        env_var: str = _KEY_ENV_VAR,
    ) -> None:
        self._runtime_root = runtime_root
        self._env_var = env_var
        self._fernet: Fernet | None = None

    # --- Public API ----------------------------------------------------------

    def encrypt(self, plaintext: str) -> str:
        """Зашифровать строку. Возвращает base64-URL-safe ASCII."""
        if not plaintext:
            return ""
        return self._fernet_instance().encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Расшифровать. Пустая строка → пустая строка (для optional полей)."""
        if not ciphertext:
            return ""
        try:
            return self._fernet_instance().decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ConflictError(
                "Не удалось расшифровать секрет: ключ шифрования изменился или "
                "данные повреждены. Если ротировали POV_SECRET_KEY — переключите "
                "обратно или пересоздайте провайдеров через UI."
            ) from exc

    # --- Internals -----------------------------------------------------------

    def _fernet_instance(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(self._resolve_key())
        return self._fernet

    def _resolve_key(self) -> bytes:
        env_key = os.environ.get(self._env_var, "").strip()
        if env_key:
            try:
                # Валидация: Fernet ругнётся, если строка не base64 32 байт.
                Fernet(env_key.encode("ascii"))
            except (ValueError, TypeError) as exc:
                raise ConflictError(
                    f"{self._env_var} задана, но не похожа на корректный Fernet-ключ "
                    "(нужно 32 байта в base64-URL-safe формате; "
                    "сгенерировать: `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`)."
                ) from exc
            return env_key.encode("ascii")

        key_path = self._runtime_root / _KEY_FILENAME
        if key_path.exists():
            data = key_path.read_bytes().strip()
            if data:
                return data

        # Первый запуск — генерируем persistent ключ.
        self._runtime_root.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        # POSIX: даём только владельцу читать/писать. На Windows os.chmod с
        # 0o600 транслируется в read-only бит; полноценный ACL — отдельно.
        try:
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Не критично: файл в gitignored директории runtime/.
            pass
        return key
