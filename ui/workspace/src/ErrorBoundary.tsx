import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Заголовок фолбэка. */
  title?: string;
  /** Доп. действие при сбросе (например, навигация на главную). */
  onReset?: () => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Граница ошибок React.
 *
 * Любой неперехваченный краш рендера в поддереве показывает дружелюбный
 * фолбэк вместо «чёрного экрана»: в React 18 необработанная ошибка
 * размонтирует ВСЁ дерево, поэтому без границы один упавший запрос/раздел
 * обнуляет весь интерфейс.
 *
 * Сброс: компонент перемонтируется при смене `key` (используется
 * `key={projectId}` вокруг рабочей области), поэтому переход на другой
 * проект автоматически очищает ошибку. Кнопки — last-resort для случаев
 * без смены маршрута.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Диагностика в консоль — фактический стек компонентов для отладки.
    console.error("ErrorBoundary поймал ошибку рендера:", error, info.componentStack);
  }

  private handleReset = (): void => {
    this.setState({ error: null });
    this.props.onReset?.();
  };

  render(): ReactNode {
    const { error } = this.state;
    if (error) {
      return (
        <div className="error-boundary" role="alert">
          <div className="error-boundary__card">
            <h2>{this.props.title ?? "Что-то пошло не так"}</h2>
            <p>
              Не удалось отобразить этот раздел. Остальная часть приложения
              продолжает работать — можно попробовать снова, вернуться к списку
              или перезагрузить страницу.
            </p>
            <pre className="error-boundary__detail">{error.message}</pre>
            <div className="error-boundary__actions">
              <button type="button" onClick={this.handleReset}>
                Попробовать снова
              </button>
              <button type="button" onClick={() => window.location.reload()}>
                Перезагрузить страницу
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
