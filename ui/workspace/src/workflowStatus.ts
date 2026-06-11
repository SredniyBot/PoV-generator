/**
 * Единый словарь статусов workflow — ОДИН источник правды для подписей, цветов
 * и тонов во всех поверхностях (граф задач, лента шагов, дорожка этапов, пилюли
 * статуса прогона).
 *
 * Раньше эти маппинги жили независимо в 4+ местах (TaskGraphCanvas.STATUS_META,
 * statusFillColor, App.labelForRunStatus/labelForStepStatus, ui.StatusPill-
 * callers) и расходились: одно состояние получало 3 разных перевода/цвета,
 * `ready` и `in_progress` были неотличимы, а статус прогона буквально выводил
 * «workflow». Здесь это сведено к чистым функциям (SRP + DRY): меняем в одном
 * месте — согласованно меняется везде.
 */

// Совпадает с union у StatusPill (ui.tsx) — структурно совместимо.
export type WorkflowTone =
  | "neutral"
  | "active"
  | "success"
  | "warning"
  | "danger"
  | "muted";

export interface StatusVisual {
  /** Человеческая подпись (русская, единый регистр). */
  label: string;
  /** Цвет для точек/полос на графе и заливки мини-карты (rgba). */
  color: string;
  /** Тон для StatusPill и текстовых акцентов. */
  tone: WorkflowTone;
  /** in_progress пульсирует — это «живой» статус. */
  pulse: boolean;
}

const NEUTRAL_COLOR = "rgba(150, 160, 180, 0.6)";
const ACCENT_COLOR = "rgba(120, 184, 201, 0.95)";
const WARNING_COLOR = "rgba(214, 173, 89, 0.9)";
const DANGER_COLOR = "rgba(215, 131, 131, 0.95)";
const SUCCESS_COLOR = "rgba(140, 196, 153, 0.9)";
const MUTED_COLOR = "rgba(150, 150, 150, 0.55)";

// Статусы задачи (domain/tasks.py). После нормализации статусов: blocked =
// «ждёт пользователя/зависимостей» (не ошибка), failed = только настоящая
// ошибка. ready визуально отличается от in_progress (Блок 7): «готова к
// запуску» — приглушённый акцент, «в работе» — полный акцент + пульс.
const TASK_STATUS: Record<string, StatusVisual> = {
  completed:                  { label: "Готово",          color: SUCCESS_COLOR, tone: "success", pulse: false },
  in_progress:                { label: "В работе",        color: ACCENT_COLOR,  tone: "active",  pulse: true },
  ready:                      { label: "Готова к запуску", color: "rgba(120, 184, 201, 0.45)", tone: "neutral", pulse: false },
  candidate:                  { label: "Запланирована",   color: NEUTRAL_COLOR, tone: "neutral", pulse: false },
  blocked:                    { label: "Заблокирована",   color: WARNING_COLOR, tone: "warning", pulse: false },
  waiting_for_children:       { label: "В процессе",      color: WARNING_COLOR, tone: "warning", pulse: false },
  waiting_for_fan_out_source: { label: "Ждёт данные",     color: WARNING_COLOR, tone: "warning", pulse: false },
  failed:                     { label: "Ошибка",          color: DANGER_COLOR,  tone: "danger",  pulse: false },
  skipped:                    { label: "Пропущена",       color: MUTED_COLOR,   tone: "muted",   pulse: false },
  obsolete:                   { label: "Устарела",        color: MUTED_COLOR,   tone: "muted",   pulse: false },
};

/** Визуал статуса задачи (граф, мини-карта, любые места со статусом задачи). */
export function taskStatusVisual(status: string): StatusVisual {
  return TASK_STATUS[status] ?? { label: status, color: NEUTRAL_COLOR, tone: "neutral", pulse: false };
}

/**
 * Честный статус прогона: статус + причина остановки → одна понятная фраза.
 * Раньше running выводился как «workflow», а остановки по решению/ошибке/
 * блокировке все показывались как «Завершено».
 */
export function runStatusVisual(
  status: string,
  stopReason?: string | null,
): { label: string; tone: WorkflowTone } {
  if (status === "running" || status === "pending") return { label: "Идёт работа", tone: "active" };
  if (status === "cancelled") return { label: "Прервано", tone: "warning" };
  if (status === "failed") return { label: "Ошибка прогона", tone: "danger" };
  if (status === "completed") {
    switch (stopReason) {
      case "objective_completed": return { label: "Этап завершён", tone: "success" };
      case "awaiting_checkpoint": return { label: "Остановлено: нужны ваши решения", tone: "warning" };
      case "validation_failed":   return { label: "Остановлено: ошибка на шаге", tone: "danger" };
      case "planner_blocked":     return { label: "Остановлено: нет следующих шагов", tone: "neutral" };
      case "max_steps_reached":   return { label: "Остановлено: достигнут лимит шагов", tone: "warning" };
      case "cancelled_by_user":   return { label: "Прервано", tone: "warning" };
      default:                    return { label: "Завершено", tone: "success" };
    }
  }
  return { label: status, tone: "neutral" };
}

/**
 * Текущее состояние проекта из «ситуации» (task-derived, живое) — для пилюли
 * в шапке, когда активного прогона нет. Источник правды для «что сейчас» — это
 * ситуация, а НЕ статус последнего завершённого прогона (тот уходит в историю
 * ленты). Так пилюля не «зависает» на устаревшем stop_reason.
 */
export function situationVisual(situation: {
  status_label: string;
  blocking: boolean;
  blockers?: { severity?: string }[];
}): { label: string; tone: WorkflowTone } {
  const label = situation.status_label || "Состояние проекта";
  if (situation.blocking) {
    const hasError = (situation.blockers ?? []).some(
      (b) => b.severity === "error" || b.severity === "critical",
    );
    return { label, tone: hasError ? "danger" : "warning" };
  }
  if (/идёт|идет|работ/i.test(label)) return { label, tone: "active" };
  if (/готов|заверш|успе/i.test(label)) return { label, tone: "success" };
  return { label, tone: "neutral" };
}

/**
 * Шаг ленты: пара (validation_status, planning_outcome) → подпись + тон,
 * в едином стиле со статусами задач.
 */
export function stepStatusVisual(
  validationStatus: string | null,
  planningOutcome: string,
): { label: string; tone: WorkflowTone } {
  if (validationStatus === "passed") return { label: "Готово", tone: "success" };
  if (validationStatus === "failed") return { label: "Ошибка", tone: "danger" };
  if (validationStatus === "paused_for_checkpoint") return { label: "Ждёт решения", tone: "warning" };
  if (planningOutcome === "selected" || planningOutcome === "retried") return { label: "Идёт", tone: "active" };
  if (planningOutcome === "objective_completed") return { label: "Цель достигнута", tone: "success" };
  if (planningOutcome === "blocked") return { label: "Заблокирована", tone: "warning" };
  return { label: planningOutcome, tone: "neutral" };
}
