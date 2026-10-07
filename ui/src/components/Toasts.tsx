import { useEffect, useRef } from 'react';
import { useI18n } from '../i18n';
import { useAppState, useDispatch } from '../state/context';
import type { Toast } from '../state/types';
import { Icon } from './Icon';
import { BidiText } from './BidiText';

const ICONS: Record<Toast['level'], string> = { info: 'info', success: 'check-circle', warning: 'alert', error: 'x-circle' };

export function toastDuration(toast: Pick<Toast, 'level' | 'message'>): number {
  const base = toast.level === 'error' ? 8000 : toast.level === 'warning' ? 6500 : 5000;
  return base + Math.min(4000, (toast.message?.length ?? 0) * 25);
}

function ToastItem({ toast }: { toast: Toast }) {
  const dispatch = useDispatch();
  const { t } = useI18n();
  const duration = toastDuration(toast);
  const remaining = useRef(duration);
  const startedAt = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const dismiss = () => dispatch({ type: 'toast.dismiss', id: toast.id });

  const start = () => {
    startedAt.current = performance.now();
    timer.current = setTimeout(dismiss, remaining.current);
  };
  const pause = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    remaining.current = Math.max(400, remaining.current - (performance.now() - startedAt.current));
  };

  useEffect(() => {
    start();
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toast.id]);

  return (
    <div className="toast" data-level={toast.level} role={toast.level === 'error' ? 'alert' : 'status'} onMouseEnter={pause} onMouseLeave={start} data-testid="toast">
      <span className="toast__icon">
        <Icon name={ICONS[toast.level]} size={18} />
      </span>
      <div className="toast__body">
        <p className="toast__title bidi">{toast.title}</p>
        {toast.message ? (
          <p className="toast__msg bidi">
            <BidiText text={toast.message} />
          </p>
        ) : null}
      </div>
      <button type="button" className="icon-btn icon-btn--sm" onClick={dismiss} aria-label={t('common.close')}>
        <Icon name="x" size={14} />
      </button>
      <span className="toast__timer" style={{ animationDuration: `${duration}ms` }} />
    </div>
  );
}

/** Notification toasts (server `notification` events and local errors). Hover pauses the timer. */
export function Toasts() {
  const toasts = useAppState((s) => s.toasts);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} />
      ))}
    </div>
  );
}
