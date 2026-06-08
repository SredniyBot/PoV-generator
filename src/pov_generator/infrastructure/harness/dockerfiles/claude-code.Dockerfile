# Образ агента Claude Code (Ф7). Самодостаточный: ставит claude CLI + git.
# node:20 (полный, не slim) — у slim не хватало системных библиотек для
# postinstall пакета (сборка падала кодом 1).
#
# ВАЖНО (архитектура): claude CLI здесь аутентифицируется НЕ через `claude login`,
# а через переменную окружения ANTHROPIC_API_KEY, которая подаётся в песочницу
# эфемерно из настроенного LLM-подключения Anthropic — это ТОТ ЖЕ аккаунт/ключ,
# что и в «Настройках → LLM», а не отдельный Claude. Ключ в образ НЕ зашивается.
FROM node:20

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit @anthropic-ai/claude-code

WORKDIR /work
