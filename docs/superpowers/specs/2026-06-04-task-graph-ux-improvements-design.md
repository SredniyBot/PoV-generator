# Task Graph UX Improvements — Design Spec

**Date:** 2026-06-04  
**Branch:** `feature/task-graph-ux-improvements`  
**Scope:** UI-only changes to `TaskGraphCanvas.tsx` and `styles.css`

---

## Context

The task graph (ReactFlow + dagre, 30–80 nodes) supports three equal use cases:
progress monitoring, failure debugging, and structure understanding. The primary
pain point is that it is unclear what is happening right now — there is no visual
focus on the currently executing task. Users also need to act (retry, skip, open
artifact, go to clarification) without leaving the graph view.

---

## Goals

1. Make the active task immediately visible on load and on every status change.
2. Surface error details without requiring a drawer open.
3. Enable contextual actions directly on nodes.

## Non-Goals

- No changes to backend API or domain layer.
- No new heavy dependencies.
- No redesign of existing node card layout beyond targeted additions.
- No collapse/expand of composite subtrees (deferred to a future iteration).

---

## Changes

### 1. Floating Progress Banner

**Component:** inline inside `TaskGraphCanvas`, absolutely positioned top-left above
ReactFlow controls.

**Content:**
- Status dot (cyan pulse if `in_progress` task exists, gray otherwise)
- Current task title (looked up from flat nodes by `current_task_id`)
- Progress counter: `completed_leaf_tasks / total_leaf_tasks`

**Visibility:** hidden when `total_leaf_tasks === 0`.

**Styling:** uses existing CSS variables (`--color-accent`, `--color-surface`,
`--color-text-secondary`). No new color values introduced.

---

### 2. Auto-Pan/Zoom to Active Node

**Location:** `useEffect` in `TaskGraphCanvasInner`, depends on the `task_id` of
the node where `is_current === true`.

**Behavior:**
- On mount and whenever the active node changes, call `setCenter` (from
  `useReactFlow`) on the active node's position with `duration: 600` and
  `zoom: 1.2`.
- Does **not** trigger on every render — only when the active node id changes.
- If no active node exists, no pan occurs.

---

### 3. Pulsing Outline on Active Node

**Location:** `styles.css`, extending the existing `.tg-node--current` rule.

**Implementation:** `@keyframes tg-pulse` animates `box-shadow` opacity from
`1 → 0.4 → 1` over 2 s, looping infinitely. Applied only to `.tg-node--current`.

---

### 4. Hover Action Menu on Node Cards

**Location:** `TaskCardNode` component, new `actions` prop section.

**Visible buttons** (conditionally rendered via a pure helper function, not inline
ternaries in JSX):

| Button | Condition |
|--------|-----------|
| Retry | `retryable === true` AND `status === "failed"` |
| Skip | `status` ∈ `{ready, blocked, candidate}` |
| Open artifact | `status === "completed"` |
| Go to clarification | `blocking_clarification_count > 0` |

**UX:** panel is `opacity: 0` at rest, transitions to `opacity: 1` on
`.tg-node:hover`. Buttons use existing icon + tooltip pattern from the project.

**Callbacks:** `onRetry`, `onSkip`, `onOpenArtifact`, `onClarification` passed
as props from `TaskGraphCanvas` down to `TaskCardNode`. `TaskGraphCanvas` already
receives `retryMutation` from `TaskGraphPage`; skip/artifact/clarification
callbacks are new and will be wired up in `TaskGraphPage`.

---

### 5. Status Summary on Failed Node

**Location:** `TaskCardNode`, below the title.

**Condition:** `status === "failed"` AND `status_summary != null`.

**Styling:** single line, `text-overflow: ellipsis`, color `--color-error`.
Full text available in the existing drawer on click.

---

## Defensive Checks

- All node lookups use optional chaining; missing `current_task_id` is a no-op.
- Action buttons guard against undefined callbacks before invoking.
- `status_summary` renders only when both conditions are met (no empty element).
- Auto-pan only fires when a valid node position exists in the ReactFlow node list.

---

## Files Changed

| File | Change |
|------|--------|
| `ui/workspace/src/TaskGraphCanvas.tsx` | Banner component, auto-pan effect, action menu props/rendering |
| `ui/workspace/src/styles.css` | `@keyframes tg-pulse`, banner styles, action menu styles, status summary styles |
| `ui/workspace/src/App.tsx` | Wire skip/artifact/clarification callbacks in `TaskGraphPage` |

---

## Testing

- Run `npm run build` (tsc + vite) to verify no type errors.
- Manually verify: active node is centred on load, pulse is visible, actions appear
  on hover and only when conditions are met, error summary appears on failed nodes.
- Existing scenarios: non-active graphs, empty graphs (`total_leaf_tasks === 0`),
  all-completed graphs — must not break.
