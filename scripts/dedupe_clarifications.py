"""B3 cleanup: схлопывает уже накопленные дубли clarification_requests.

Запуск:
    python scripts/dedupe_clarifications.py runtime/ui_cases/<workspace>
    python scripts/dedupe_clarifications.py --all     # все workspaces в runtime/

Логика (соответствует B3 layer 2):
1. Группируем все clarification_requests в workspace по
   `(project_id, normalized_question)`.
2. В каждой группе если есть ≥1 `answered`/`assumed` request — все
   `open` дубли переводятся в `deferred` с reason
   `resolved_via:{id_первого_answered}`. Пишется audit event.
3. Если в группе только `open` дубли (никто ещё не отвечал) — оставляем
   как есть (пользователь сам выберет на какой отвечать; layer 2 потом
   подберёт остальные).

Скрипт идемпотентен: повторный запуск ничего не ломает.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path


_PUNCT_RE = re.compile(r"[.,!?;:…]+$")


def normalize(question: str | None) -> str:
    if not question:
        return ""
    s = " ".join(question.casefold().split())
    return _PUNCT_RE.sub("", s).strip()


def dedupe_workspace(workspace: Path, dry_run: bool = False) -> dict:
    db_path = workspace / "runtime.db"
    if not db_path.exists():
        return {"workspace": str(workspace), "skipped": "no runtime.db"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Старые workspace могут не иметь таблицы (создавалась миграциями
        # рантайма); пропускаем.
        has_table = conn.execute(
            "select 1 from sqlite_master where type='table' and name='clarification_requests'"
        ).fetchone()
        if not has_table:
            return {"workspace": str(workspace), "skipped": "no clarification_requests table"}
        rows = list(
            conn.execute(
                """
                select request_id, project_id, status, question, created_at, updated_at
                from clarification_requests
                where status in ('open', 'answered', 'assumed', 'deferred')
                """
            ).fetchall()
        )
        # Группируем по (project_id, normalized_question)
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (row["project_id"], normalize(row["question"]))
            groups.setdefault(key, []).append(row)

        merged = 0
        for (project_id, nq), group in groups.items():
            if not nq or len(group) < 2:
                continue
            # Есть ли в группе уже-разрешённый request?
            resolved = [r for r in group if r["status"] in ("answered", "assumed")]
            if not resolved:
                continue
            primary = resolved[0]
            # Закрываем все open в этой группе как deferred via primary
            for row in group:
                if row["status"] != "open":
                    continue
                reason = f"resolved_via:{primary['request_id']}"
                if dry_run:
                    print(
                        f"  DRY: would defer {row['request_id'][:8]} → via {primary['request_id'][:8]} "
                        f"(question: {nq[:50]}…)"
                    )
                else:
                    conn.execute(
                        """
                        update clarification_requests
                        set status = 'deferred',
                            resolution_summary = ?,
                            updated_at = datetime('now')
                        where request_id = ?
                        """,
                        (reason, row["request_id"]),
                    )
                    conn.execute(
                        """
                        insert into clarification_events
                          (event_id, request_id, project_id, event_type, payload_json, actor, created_at)
                        values (lower(hex(randomblob(16))), ?, ?, 'deferred', ?, 'cleanup_script', datetime('now'))
                        """,
                        (
                            row["request_id"],
                            row["project_id"],
                            json.dumps(
                                {
                                    "reason": reason,
                                    "previous_status": "open",
                                    "via_request_id": primary["request_id"],
                                    "auto": True,
                                    "source": "cleanup_script",
                                }
                            ),
                        ),
                    )
                merged += 1
        if not dry_run:
            conn.commit()
        return {"workspace": str(workspace), "merged": merged, "groups_total": len(groups)}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", nargs="?", help="path to single workspace dir")
    parser.add_argument(
        "--all",
        action="store_true",
        help="process all workspaces under runtime/ui_cases (and other subdirs)",
    )
    parser.add_argument("--runtime-root", default="runtime", help="runtime root dir")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be done without writing"
    )
    args = parser.parse_args()

    targets: list[Path] = []
    if args.workspace:
        targets.append(Path(args.workspace))
    elif args.all:
        root = Path(args.runtime_root)
        for db in root.rglob("runtime.db"):
            targets.append(db.parent)
    else:
        parser.error("укажите workspace или --all")

    total_merged = 0
    for ws in targets:
        result = dedupe_workspace(ws, dry_run=args.dry_run)
        if "skipped" in result:
            continue
        print(f"{ws}: merged={result['merged']} groups={result['groups_total']}")
        total_merged += result["merged"]
    print(f"\nИтого {'(dry-run)' if args.dry_run else ''}: merged={total_merged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
