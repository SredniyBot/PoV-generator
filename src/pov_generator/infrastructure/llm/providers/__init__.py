"""Адаптеры конкретных LLM-провайдеров под :class:`LLMProvider`.

Каждый файл — тонкая обёртка над соответствующим клиентом
(``OpenRouterClient`` / ``ClaudeSdkClient`` / ``ClaudeSubscriptionClient``):
конструктор подхватывает env через ``from_env(...)`` исходного клиента,
``chat_json`` пересылает аргументы как есть.
"""
