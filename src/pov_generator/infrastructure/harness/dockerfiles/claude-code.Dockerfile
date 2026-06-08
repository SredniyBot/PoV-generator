# Образ агента Claude Code (Ф7). Самодостаточный: ставит claude CLI + git.
# Креды (ANTHROPIC_API_KEY) подаются в песочницу эфемерно на время прогона, в
# образ НЕ зашиваются. Egress контролируется песочницей (deny-by-default).
FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /work
