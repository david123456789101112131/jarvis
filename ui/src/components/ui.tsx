import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { Icon } from './Icon';
import { LIVE_STATUSES, type Tone } from './tones';

interface PanelProps {
  title: ReactNode;
  icon?: string;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  flush?: boolean;
  testId?: string;
  style?: CSSProperties;
  footer?: ReactNode;
  as?: 'section' | 'div' | 'article';
  labelledBy?: string;
}

/** Glass HUD panel with corner brackets, a header and a scrollable body. */
export function Panel({ title, icon, meta, actions, children, className, bodyClassName, flush, testId, style, footer, as = 'section' }: PanelProps) {
  const id = useId();
  const Tag = as;
  return (
    <Tag className={`panel ${className ?? ''}`} data-testid={testId} style={style} aria-labelledby={`${id}-t`}>
      <header className="panel__head">
        <h2 className="panel__title" id={`${id}-t`}>
          {icon ? (
            <span className="panel__title-icon">
              <Icon name={icon} size={13} />
            </span>
          ) : null}
          <span className="truncate">{title}</span>
        </h2>
        {meta ? <span className="panel__meta">{meta}</span> : null}
        {actions ? <div className="panel__actions">{actions}</div> : null}
      </header>
      <div className={`panel__body ${flush ? 'panel__body--flush' : ''} ${bodyClassName ?? ''}`}>{children}</div>
      {footer ? <footer className="panel__foot">{footer}</footer> : null}
    </Tag>
  );
}

export function Chip({ tone = 'steel', children, dot, pulse, size, title, className, testId }: { tone?: Tone; children: ReactNode; dot?: boolean; pulse?: boolean; size?: 'lg'; title?: string; className?: string; testId?: string }) {
  return (
    <span
      className={`chip ${dot ? 'chip--dot' : ''} ${pulse ? 'chip--pulse' : ''} ${size === 'lg' ? 'chip--lg' : ''} ${className ?? ''}`}
      data-tone={tone}
      title={title}
      data-testid={testId}
    >
      {children}
    </span>
  );
}

export function StatusChip({ status, tone, label, size, testId }: { status: string; tone: Tone; label: string; size?: 'lg'; testId?: string }) {
  return (
    <Chip tone={tone} dot pulse={LIVE_STATUSES.has(status)} size={size} testId={testId}>
      {label}
    </Chip>
  );
}

export function ProgressBar({ value, tone, variant, label, indeterminate }: { value: number; tone?: Tone; variant?: 'thin' | 'thick'; label?: string; indeterminate?: boolean }) {
  const pct = Math.round(Math.max(0, Math.min(1, value || 0)) * 100);
  return (
    <div
      className={`progress ${variant ? `progress--${variant}` : ''} ${indeterminate ? 'progress--indeterminate' : ''}`}
      data-tone={tone}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : pct}
      aria-label={label}
    >
      <div className="progress__fill" style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Toggle({ checked, onChange, label, disabled, testId, hideLabel }: { checked: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean; testId?: string; hideLabel?: boolean }) {
  return (
    <span
      className="toggle"
      role="switch"
      aria-checked={checked}
      aria-disabled={disabled || undefined}
      aria-label={hideLabel ? label : undefined}
      tabIndex={disabled ? -1 : 0}
      data-testid={testId}
      onClick={() => !disabled && onChange(!checked)}
      onKeyDown={(e) => {
        if (disabled) return;
        if (e.key === ' ' || e.key === 'Enter') {
          e.preventDefault();
          onChange(!checked);
        }
      }}
    >
      {hideLabel ? null : <span>{label}</span>}
      <span className="toggle__track" aria-hidden="true">
        <span className="toggle__thumb" />
      </span>
    </span>
  );
}

export function Slider({ value, min, max, step, onChange, label, format, testId }: { value: number; min: number; max: number; step: number; onChange: (v: number) => void; label: string; format?: (v: number) => string; testId?: string }) {
  const pct = ((value - min) / (max - min)) * 100;
  return (
    <input
      type="range"
      className="slider"
      min={min}
      max={max}
      step={step}
      value={value}
      aria-label={label}
      aria-valuetext={format ? format(value) : undefined}
      data-testid={testId}
      style={{ '--pct': `${pct}%` } as CSSProperties}
      onChange={(e) => onChange(Number(e.target.value))}
    />
  );
}

export function Segmented<T extends string>({ value, options, onChange, label, testId, disabled }: { value: T; options: { value: T; label: string; title?: string }[]; onChange: (v: T) => void; label: string; testId?: string; disabled?: boolean }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const index = options.findIndex((o) => o.value === value);
  const move = (delta: number) => {
    const next = (index + delta + options.length) % options.length;
    onChange(options[next].value);
    refs.current[next]?.focus();
  };
  return (
    <div className="segmented" role="radiogroup" aria-label={label} data-testid={testId}>
      {options.map((o, i) => (
        <button
          key={o.value}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          role="radio"
          className="segmented__item"
          aria-checked={o.value === value}
          tabIndex={o.value === value || (index < 0 && i === 0) ? 0 : -1}
          title={o.title ?? o.label}
          disabled={disabled}
          data-value={o.value}
          onClick={() => onChange(o.value)}
          onKeyDown={(e) => {
            const rtl = document.documentElement.dir === 'rtl';
            if (e.key === 'ArrowRight') {
              e.preventDefault();
              move(rtl ? -1 : 1);
            } else if (e.key === 'ArrowLeft') {
              e.preventDefault();
              move(rtl ? 1 : -1);
            }
          }}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function EmptyState({ icon = 'info', title, hint, children, testId }: { icon?: string; title: string; hint?: string; children?: ReactNode; testId?: string }) {
  return (
    <div className="empty" data-testid={testId}>
      <span className="empty__icon">
        <Icon name={icon} size={18} />
      </span>
      <span className="empty__title">{title}</span>
      {hint ? <span className="empty__hint">{hint}</span> : null}
      {children}
    </div>
  );
}

export function ErrorBox({ message, onRetry, retryLabel }: { message: string; onRetry?: () => void; retryLabel?: string }) {
  return (
    <div className="error-box" role="alert">
      <Icon name="alert" size={16} />
      <span style={{ flex: 1 }}>{message}</span>
      {onRetry ? (
        <button type="button" className="btn btn--sm btn--danger" onClick={onRetry}>
          {retryLabel ?? 'Retry'}
        </button>
      ) : null}
    </div>
  );
}

export function Loading({ label, rows = 3 }: { label: string; rows?: number }) {
  return (
    <div aria-busy="true" aria-label={label} style={{ display: 'grid', gap: 10, padding: '4px 0' }}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ height: 14, width: `${88 - i * 14}%` }} />
      ))}
    </div>
  );
}

export function AgentBadge({ agent, number, size }: { agent: string; number: number | string; size?: 'sm' | 'lg' }) {
  return (
    <span className={`agent-badge ${size ? `agent-badge--${size}` : ''}`} data-agent={agent} aria-hidden="true">
      {number}
    </span>
  );
}

/**
 * A button that asks for confirmation inline before running a destructive action.
 * First click arms it; the second click within 4 s confirms.
 */
export function ConfirmButton({ children, confirmLabel, onConfirm, className, disabled, title, testId }: { children: ReactNode; confirmLabel: string; onConfirm: () => void; className?: string; disabled?: boolean; title?: string; testId?: string }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button
      type="button"
      className={`${className ?? 'btn btn--sm'} ${armed ? 'btn--danger' : ''}`}
      disabled={disabled}
      title={title}
      data-testid={testId}
      onClick={() => {
        if (armed) {
          setArmed(false);
          onConfirm();
        } else setArmed(true);
      }}
      onBlur={() => setArmed(false)}
    >
      {armed ? confirmLabel : children}
    </button>
  );
}
