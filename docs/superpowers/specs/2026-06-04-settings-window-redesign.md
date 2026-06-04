# Settings Window Redesign — Design Spec

**Date:** 2026-06-04
**Scope:** `ui/workspace/src/LlmSettingsPage.tsx` + `ui/workspace/src/styles.css`
**Goal:** Привести окно настроек к продуктовому уровню — понятная структура, аккуратный UI, предсказуемое поведение, хорошие состояния загрузки/ошибок, удобное редактирование.

---

## 1. Архитектура

Все изменения в двух файлах. Новые примитивы объявляются прямо в `LlmSettingsPage.tsx` — они специфичны для этой страницы.

### Новые компоненты

| Компонент | Назначение |
|-----------|------------|
| `SettingsModal` | Backdrop + card (480px) + close-on-Escape + click-outside |
| `useToast` + `ToastContainer` | Стек до 3 уведомлений, auto-dismiss 4s |
| `DeleteConfirm` | Inline-подтверждение удаления, заменяет строку |
| `SkeletonRow` | Shimmer-placeholder для loading state |

### Изменения существующих компонентов

| Компонент | Что меняется |
|-----------|-------------|
| `LlmSettingsPage` | Tab-badges с `⚠` для untested/missing |
| `ProviderRow` | Collapsible expanded section, inline delete confirm, badge рядом с именем |
| `ProvidersTab` | Form → Modal, скелетоны, error state |
| `ModelsTab` | Search input, form → Modal, скелетоны, error state |
| `AssignmentsTab` | Optimistic updates, скелетоны, error state |

### Что не меняется

- `api.ts`, `types.ts`, `App.tsx`
- 3-табовая структура
- Маршрут `/settings`

---

## 2. ProviderRow

### Collapsed (по умолчанию)

```
┌─────────────────────────────────────────────────────────────────┐
│ ● Anthropic prod     [работает ✓]    [Проверить] [▼]  [🗑]     │
│   Anthropic API · ключ sk-ant-…••••                             │
└─────────────────────────────────────────────────────────────────┘
```

### Expanded

```
┌─────────────────────────────────────────────────────────────────┐
│ ● Anthropic prod     [работает ✓]    [Проверить] [▲]  [🗑]     │
│   Anthropic API · ключ sk-ant-…••••                             │
│  ─────────────────────────────────────────────────────────────  │
│  Параллельных шагов: [авто ▾]    [↻ Обновить каталог]          │
│  Последний тест: 245ms · "Hello!" — 2 ч назад                  │
└─────────────────────────────────────────────────────────────────┘
```

### Delete confirm

```
┌─────────────────────────────────────────────────────────────────┐
│  Удалить «Anthropic prod»?              [Отмена]  [Удалить]     │
└─────────────────────────────────────────────────────────────────┘
```

Вся строка заменяется `DeleteConfirm`. Отмена возвращает строку. Confirm — вызывает мутацию + показывает toast.

### Детали

- Статус-badge (`работает / ошибка / не протестирован`) — рядом с именем, не в отдельной колонке
- `concurrency` → `<select>` (авто / 1 … 16), compact, в expanded-секции
- «Обновить каталог» переезжает в expanded — редкое действие
- Test result сохраняется в локальном state до следующего теста (не исчезает при ре-рендере)
- `onBlur`-сохранение concurrency заменяется на `onChange` → `<select>`, сохраняется сразу

---

## 3. Modal формы

### `SettingsModal`

```tsx
<SettingsModal title="..." onClose={...}>
  {/* form content */}
</SettingsModal>
```

- Backdrop `rgba(0,0,0,0.6)` с `backdrop-filter: blur(4px)`
- Карточка 480px, border-radius 16px, в стиле тёмной темы проекта
- Закрытие: Escape, клик на backdrop, кнопка ✕
- `NewProviderForm` и `AddCustomModelForm` рендерятся внутри

### Триггеры

- «Подключить источник» → Modal с `NewProviderForm`
- «Добавить свою модель» → Modal с `AddCustomModelForm`

---

## 4. Loading / Error states

### Loading

`SkeletonRow` — placeholder-карточка с shimmer-анимацией. Рендерим 3 штуки пока `isLoading`.

```css
@keyframes shimmer {
  from { background-position: -200% 0; }
  to   { background-position:  200% 0; }
}
.sk-row {
  height: 72px;
  border-radius: 12px;
  background: linear-gradient(90deg,
    rgba(255,255,255,0.04) 25%,
    rgba(255,255,255,0.08) 50%,
    rgba(255,255,255,0.04) 75%
  );
  background-size: 200% 100%;
  animation: shimmer 1.4s ease infinite;
}
```

### Error state

Карточка с иконкой `AlertCircle`, текстом ошибки и кнопкой «Повторить» (`query.refetch()`). Применяется если `isError` в любом табе.

---

## 5. Toast

### API

```tsx
const { toast } = useToast();
toast({ type: "success", message: "Подключение удалено" });
toast({ type: "error",   message: "Не удалось сохранить" });
```

### Поведение

- Позиция: bottom-right, 16px от края
- Стек до 3 уведомлений; новые пушатся снизу
- Auto-dismiss 4s (success) / 6s (error)
- Кнопка ✕ для ручного закрытия
- Slide-in анимация снизу

### Когда показывается

| Событие | Toast |
|---------|-------|
| Провайдер создан | success «Подключение добавлено» |
| Провайдер удалён | success «Подключение удалено» |
| Тест провайдера ок | success «Соединение работает · Xms» |
| Тест провайдера ошибка | error «Ошибка: {message}» |
| Синхронизация каталога | success «Добавлено N моделей» |
| Routing удалён | success «Маршрут удалён» |
| Назначение сохранено | success «Сохранено» |
| Назначение — ошибка | error «Не удалось сохранить» |
| Сброс к рекомендуемым | success «Назначения сброшены» |
| Любая мутация — error | error с сообщением из exception |

---

## 6. Tab badges

`LlmSettingsPage` вычисляет:

```ts
const untestedCount  = providers.filter(p => p.last_test_status !== "ok").length;
const missingCount   = assignments.filter(a => !availableModels.includes(a.model_name)).length;
```

- `untestedCount > 0` → amber-точка `●` рядом с «Источники»
- `missingCount > 0` → amber-точка рядом с «Назначения»

Данные берутся из уже загруженных queries (нет дополнительных запросов).

---

## 7. Models tab — поиск

Search input над списком:

```
[🔍 Поиск по имени модели...]
```

- `<input type="search">`, local state `query`
- Фильтрация: `models.filter(m => m.model_name.toLowerCase().includes(query.toLowerCase()))`
- Если `query` непустой и нет результатов → empty state «Нет моделей по запросу»
- Поиск работает только на клиенте, нет нового API-запроса

---

## 8. Assignments — optimistic updates

```ts
// local state зеркалит assignments
const [localAssignments, setLocalAssignments] = useState(assignmentsByPurpose);

const setMutation = useMutation({
  mutationFn: ...,
  onMutate: ({ purpose, modelName }) => {
    const prev = localAssignments[purpose];
    setLocalAssignments(a => ({ ...a, [purpose]: modelName }));
    return { prev, purpose };
  },
  onError: (_, __, ctx) => {
    setLocalAssignments(a => ({ ...a, [ctx.purpose]: ctx.prev }));
    toast({ type: "error", message: "Не удалось сохранить" });
  },
  onSuccess: () => {
    toast({ type: "success", message: "Сохранено" });
    invalidateAll();
  },
});
```

Select меняется мгновенно; при ошибке — откат. `localAssignments` синхронизируется с `assignmentsQuery.data` через `useEffect`, чтобы после `invalidateAll()` локальный state не расходился с сервером.

---

## 9. Empty states с onboarding

### Источники — пусто

```
┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
  🔌  Нет подключений
  Добавьте хотя бы один источник — без него
  workflow не запустится.
                        [+ Подключить источник]
└ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
```

### Модели — пусто

```
┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
  📦  Каталог пуст
  1. Перейдите на вкладку «Источники»
  2. Подключите провайдера
  3. Нажмите ↻ Обновить каталог в строке провайдера
└ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
```

### Назначения — нет моделей

```
┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
  ⚙  Сначала добавьте модели
  Перейдите в «Источники» → подключите провайдера
  → «Обновить каталог». Затем вернитесь сюда.
└ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
```

---

## 10. CSS — новые блоки

| Блок | Назначение |
|------|------------|
| `.settings-modal` / `.settings-modal__card` | Modal overlay + карточка |
| `.toast-container` / `.toast` / `.toast--success` / `.toast--error` | Toast stack |
| `.sk-row` + `@keyframes shimmer` | Skeleton loader |
| `.llm-tab__badge` | Amber-точка на табе |
| `.llm-row__expand-btn` | Кнопка ▼/▲ |
| `.llm-row__details` | Collapsible секция (height transition) |
| `.llm-row__delete-confirm` | Inline confirm block |
| `.llm-search` | Search input в ModelsTab |

Все новые стили добавляются в конец секции `/* Settings */` в `styles.css`.

---

## 11. File map

| Файл | Изменение |
|------|-----------|
| `ui/workspace/src/LlmSettingsPage.tsx` | Полная переработка, +700 строк net |
| `ui/workspace/src/styles.css` | +~120 строк новых классов |

---

## 12. Out of scope

- `api.ts`, `types.ts` — не трогаем
- `App.tsx` — не трогаем
- Backend — не трогаем
- Мобильная адаптация AssignmentsTab (таблица остаётся таблицей)
- Drag-and-drop для routing priority (остаются стрелки)
