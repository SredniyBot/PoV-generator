/**
 * L6-2: Home dashboard (P4 status-first navigation).
 *
 * Корневой экран `/`: проекты сгруппированы по статусу.
 * Закрывает M-J4 «распределить внимание между 3–5 параллельными»
 * и M-J7 «войти в контекст проекта быстро после паузы».
 *
 * Группы (порядок по убыванию срочности):
 *   🔴 Требуют внимания   — has_blockers
 *   🟢 Идут сами          — running / в работе
 *   ✅ Готовы к передаче  — completed / done
 *   📦 Другие             — fallback
 *
 * Группировка делается на UI стороне поверх ProjectListItemView,
 * чтобы не требовать backend-миграции. Backend signal — has_blockers
 * (точный) + status_label (heuristic match).
 */
import { useMemo } from "react";
import { AlertCircle, CheckCircle2, Circle, CircleDot, type LucideIcon } from "lucide-react";

import type { ProjectListItemView } from "./types";

interface ProjectsHomeDashboardProps {
  projects: ProjectListItemView[];
  onCreate: () => void;
  onOpenProject: (projectId: string) => void;
}

type GroupId = "attention" | "running" | "ready" | "other";

interface GroupSpec {
  id: GroupId;
  Icon: LucideIcon;
  title: string;
  helper: string;
  tone: "danger" | "accent" | "success" | "muted";
}

const GROUP_ORDER: GroupSpec[] = [
  {
    id: "attention",
    Icon: AlertCircle,
    title: "Требуют вашего внимания",
    helper: "Заблокированы — система не может продолжить без вас.",
    tone: "danger",
  },
  {
    id: "running",
    Icon: CircleDot,
    title: "Идут сами",
    helper: "Система продолжает работать. Можно вернуться позже.",
    tone: "accent",
  },
  {
    id: "ready",
    Icon: CheckCircle2,
    title: "Готовы к передаче",
    helper: "Артефакты собраны. Можно принять и отдать команде.",
    tone: "success",
  },
  {
    id: "other",
    Icon: Circle,
    title: "В работе",
    helper: "Запущены, но прямо сейчас без блокеров и без активной работы.",
    tone: "muted",
  },
];

export function ProjectsHomeDashboard({
  projects,
  onCreate,
  onOpenProject,
}: ProjectsHomeDashboardProps) {
  const groups = useMemo(() => groupProjects(projects), [projects]);

  return (
    <section className="home-dash">
      <header className="home-dash__header">
        <div className="home-dash__title-block">
          <p className="home-dash__eyebrow">Мои проекты</p>
          <h1 className="home-dash__title">
            {projects.length === 1 ? "1 проект" : `${projects.length} проектов`}
            {projects.length > 0 && (
              <span className="home-dash__title-meta">
                · {groups.get("attention")?.length ?? 0} ждут вас
              </span>
            )}
          </h1>
        </div>
        <button type="button" className="home-dash__primary-cta" onClick={onCreate}>
          + Новый проект
        </button>
      </header>

      <div className="home-dash__groups">
        {GROUP_ORDER.map((spec) => {
          const groupProjectsList = groups.get(spec.id) ?? [];
          if (groupProjectsList.length === 0) {
            return null;
          }
          return (
            <section
              key={spec.id}
              className={`home-dash__group home-dash__group--${spec.tone}`}
              aria-labelledby={`group-${spec.id}`}
            >
              <header className="home-dash__group-header">
                <h2 id={`group-${spec.id}`} className="home-dash__group-title">
                  <spec.Icon size={16} aria-hidden className="home-dash__group-icon" />
                  <span>{spec.title}</span>
                  <span className="home-dash__group-counter">
                    {groupProjectsList.length}
                  </span>
                </h2>
                <p className="home-dash__group-helper">{spec.helper}</p>
              </header>

              <ul className="home-dash__cards" role="list">
                {groupProjectsList.map((project) => (
                  <ProjectCard
                    key={project.project_id}
                    project={project}
                    onClick={() => onOpenProject(project.project_id)}
                  />
                ))}
              </ul>
            </section>
          );
        })}
      </div>
    </section>
  );
}

// ---- helpers ----

function groupProjects(
  projects: ProjectListItemView[],
): Map<GroupId, ProjectListItemView[]> {
  const groups = new Map<GroupId, ProjectListItemView[]>();
  for (const spec of GROUP_ORDER) {
    groups.set(spec.id, []);
  }
  for (const project of projects) {
    const id = classify(project);
    groups.get(id)!.push(project);
  }
  // sort within groups: by updated_at desc
  for (const list of groups.values()) {
    list.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  }
  return groups;
}

function classify(project: ProjectListItemView): GroupId {
  if (project.has_blockers) {
    return "attention";
  }
  const label = (project.status_label || "").toLowerCase();
  if (/running|active|в работе|идёт|работа/i.test(label)) {
    return "running";
  }
  if (/complete|done|готов|заверш/i.test(label)) {
    return "ready";
  }
  return "other";
}

interface ProjectCardProps {
  project: ProjectListItemView;
  onClick: () => void;
}

function ProjectCard({ project, onClick }: ProjectCardProps) {
  const updated = formatUpdated(project.updated_at);
  return (
    <li className="home-card">
      <button type="button" className="home-card__button" onClick={onClick}>
        <header className="home-card__head">
          <h3 className="home-card__name">{project.name}</h3>
          <span className={`home-card__badge home-card__badge--${badgeTone(project)}`}>
            {project.status_label || "—"}
          </span>
        </header>
        {project.current_step_title && (
          <p className="home-card__step">
            <span className="home-card__step-eyebrow">Сейчас:</span>{" "}
            {project.current_step_title}
          </p>
        )}
        <footer className="home-card__foot">
          <span className="home-card__updated">{updated}</span>
          {project.has_blockers && (
            <span className="home-card__alert">Заблокирован</span>
          )}
        </footer>
      </button>
    </li>
  );
}

function badgeTone(project: ProjectListItemView): "danger" | "accent" | "success" | "muted" {
  if (project.has_blockers) return "danger";
  const label = (project.status_label || "").toLowerCase();
  if (/running|active|в работе|идёт|работа/i.test(label)) return "accent";
  if (/complete|done|готов|заверш/i.test(label)) return "success";
  return "muted";
}

function formatUpdated(iso: string | undefined): string {
  if (!iso) return "";
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    const today = new Date();
    const sameDay =
      dt.getFullYear() === today.getFullYear() &&
      dt.getMonth() === today.getMonth() &&
      dt.getDate() === today.getDate();
    if (sameDay) {
      return `сегодня ${dt.toLocaleTimeString("ru-RU", {
        hour: "2-digit",
        minute: "2-digit",
      })}`;
    }
    return dt.toLocaleDateString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}
