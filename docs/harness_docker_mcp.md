# Harness: Docker MCP для код-агента (host-режим)

Подход **B** (выбран осознанно): harness-агент `claude_code` работает на ХОСТЕ
(уже авторизован вашей подпиской через `claude login`), а сборку/тесты/запуск
кода гоняет в контейнерах через **Docker MCP**. Так агент получает цикл «написал
→ прогнал тест → увидел провал → починил», не требуя ни сборки образа с claude
внутри, ни монтирования кредов.

> ⚠️ **Безопасность.** Полный Docker MCP по возможностям ≈ доступ к хосту (через
> `docker run -v /:/host` агент может выйти за песочницу). Это осознанный
> компромисс ради простоты и переиспользования подписки — включайте только для
> доверенных агентов на своей машине. Для строгой изоляции нужен агент-в-контейнере
> (другой движок), здесь он не используется.

## Как включить

1. **Подготовьте Docker MCP-сервер.** Канонично — официальный Docker MCP Gateway
   (Docker Desktop 4.62+ / плагин `docker mcp`). Проверьте: `docker mcp gateway run --help`.

2. **Создайте файл MCP-конфига** (формат claude CLI). Путь — в POSIX-форме для
   bash на хосте (напр. `/c/Users/<вы>/.povgen/docker-mcp.json`):

   ```json
   {
     "mcpServers": {
       "docker": {
         "type": "stdio",
         "command": "docker",
         "args": ["mcp", "gateway", "run"],
         "timeout": 600000
       }
     }
   }
   ```

3. **Укажите путь в окружении** сервера `povgen-api`:

   ```
   POV_HARNESS_DOCKER_MCP_CONFIG=/c/Users/<вы>/.povgen/docker-mcp.json
   ```

4. **Harness — host-режим** (`engine=host`) для `claude_code` (в `/settings`).
   MCP подключается только в host-режиме; в docker-движке это был бы
   docker-in-docker, поэтому он там игнорируется.

## Что делает адаптер

Для host-агента при заданном `POV_HARNESS_DOCKER_MCP_CONFIG` команда получает:

```
claude -p ... --permission-mode acceptEdits \
  --mcp-config "<ваш-конфиг>" --strict-mcp-config \
  --allowedTools "mcp__*" \
  --disallowedTools "Bash"
```

- `--strict-mcp-config` — грузится ТОЛЬКО ваш docker-конфиг (чужие MCP из
  `~/.claude.json` не утаскиваются в автономного агента).
- `--allowedTools "mcp__*"` — docker-инструменты предразрешены (в headless нет
  интерактивного подтверждения).
- `--disallowedTools "Bash"` — host-shell запрещён: исполнение идёт через docker,
  а не напрямую на хосте. Файлы агент пишет через acceptEdits.

## Полезные env (claude CLI)

- `MCP_TIMEOUT` — таймаут старта MCP-сервера (мс, дефолт ~5000; docker gateway
  бывает медленным — поднимите при «MCP not connected»).
- `MAX_MCP_OUTPUT_TOKENS` — лимит вывода инструмента (дефолт ~10000).

## Откат

Уберите `POV_HARNESS_DOCKER_MCP_CONFIG` — адаптер вернётся к прежнему поведению
(агент пишет файлы, без docker-инструментов).
