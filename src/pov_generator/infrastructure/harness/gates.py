"""Гейты «готово» (Definition-of-Done) для harness-узлов (Ф5c).

После того как агент произвёл результат в песочнице, объявленные шаблоном гейты
проверяют его прямо там же: команда (build/test/lint) запускается в той же
песочнице; успех = ``exit 0`` в пределах таймаута. Провал любого гейта означает,
что узел не достиг DoD — артефакт не принимается.

Сборка Docker-образа (kaniko) — частный случай гейта: команда, собирающая
Dockerfile без демона; здесь это просто ещё одна команда-проверка.
"""

from __future__ import annotations

from .protocol import GateResult, HarnessGate
from .sandbox import LogSink, SandboxHandle, SandboxRuntime, shell_argv


def run_gates(
    sandbox: SandboxRuntime,
    handle: SandboxHandle,
    gates: tuple[HarnessGate, ...],
    *,
    default_timeout_s: int | None = None,
    on_log: LogSink | None = None,
) -> tuple[GateResult, ...]:
    """Прогнать гейты в песочнице по порядку, вернуть результат каждого.

    Не бросает на провале гейта — возвращает ``passed=False`` (решение, что
    делать с провалом, принимает вызывающий слой). Все гейты выполняются (для
    полноты отчёта), даже если ранний уже упал.
    """
    results: list[GateResult] = []
    for gate in gates:
        argv = (
            shell_argv(gate.command)
            if isinstance(gate.command, str)
            else list(gate.command)
        )
        exec_result = sandbox.exec(
            handle,
            argv,
            timeout_s=gate.timeout_s or default_timeout_s,
            on_log=on_log,
        )
        passed = (not exec_result.timed_out) and exec_result.exit_code == 0
        results.append(
            GateResult(
                name=gate.name,
                passed=passed,
                exit_code=exec_result.exit_code,
                log=exec_result.stdout,
            )
        )
    return tuple(results)
