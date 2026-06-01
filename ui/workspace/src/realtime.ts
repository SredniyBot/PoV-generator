import type { WorkflowRunView } from "./types";

/**
 * refetchInterval для запроса активного workflow-рана.
 *
 * Источник правды о прогрессе — WebSocket: запись runner'а в БД меняет
 * realtime_token, и WS инвалидирует `workflow_runs`-проекцию. Этот полл —
 * лишь тонкая страховка на критическом окне: пока run идёт, опрашиваем раз
 * в 1.5с (на случай потери одной WS-нотификации, чтобы прогресс-бар не
 * «замёрз»). На простое (рана нет / он завершён) — `false`: ноль холостого
 * трафика, полагаемся на WS.
 *
 * Структурный тип параметра вместо дженериков React Query — фактический
 * объект Query подходит по форме (есть `state.data`).
 */
export function activeRunRefetchInterval(
  query: { state: { data?: WorkflowRunView | null } },
): number | false {
  const status = query.state.data?.status;
  return status === "running" || status === "pending" ? 1500 : false;
}
