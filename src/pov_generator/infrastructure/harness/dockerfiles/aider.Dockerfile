# Образ агента Aider (Ф7). Самодостаточный: ставит aider + git (нужен для
# diff-harvest). Креды модели подаются в песочницу эфемерно на время прогона,
# в образ НЕ зашиваются. Egress контролируется песочницей (deny-by-default).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir aider-chat

WORKDIR /work
