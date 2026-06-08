# AGENTS.md (фикстура stub-harness)

Общее ядро проекта. Правила для агентов реализации:

- Read-only: `/contracts/`, `CONVENTIONS.md`, `STACK.md`, `docker-compose.yml`.
- Пиши только в зону своего сервиса: `services/<id>/`.
- Строй против контрактов из `/contracts/`, не против чужого кода.
- Команда запуска системы: `docker compose up --build`.
