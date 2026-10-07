import { useId, type ReactNode } from 'react';
import type { Tone } from './tones';

/** Build an SVG path for values in a width×height box (min..max mapped bottom..top). */
export function sparkPath(values: readonly number[], width: number, height: number, min: number, max: number, pad = 1.5): { line: string; area: string } {
  if (!values.length) return { line: '', area: '' };
  const span = max - min || 1;
  const n = values.length;
  const step = n > 1 ? width / (n - 1) : width;
  const pts = values.map((v, i) => {
    const x = n > 1 ? i * step : width;
    const clamped = Math.max(min, Math.min(max, Number.isFinite(v) ? v : min));
    const y = pad + (height - pad * 2) * (1 - (clamped - min) / span);
    return [Math.round(x * 10) / 10, Math.round(y * 10) / 10] as const;
  });
  const line = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x} ${y}`).join(' ');
  const first = pts[0];
  const lastPt = pts[pts.length - 1];
  const area = `${line} L${lastPt[0]} ${height} L${first[0]} ${height} Z`;
  return { line, area };
}

interface SparklineProps {
  values: readonly number[];
  /** Fixed scale; when omitted the max of the data (at least `floor`) is used. */
  min?: number;
  max?: number;
  floor?: number;
  tone?: Tone;
  height?: number;
  className?: string;
  /** A second series drawn dimmer (e.g. upload next to download). */
  secondary?: readonly number[];
  label?: string;
}

/** Responsive sparkline (stretches to its container width). */
export function Sparkline({ values, min = 0, max, floor = 1, tone = 'accent', height = 28, className, secondary, label }: SparklineProps) {
  const id = useId().replace(/:/g, '');
  const W = 100;
  const top = max ?? Math.max(floor, ...values, ...(secondary ?? []));
  const main = sparkPath(values, W, height, min, top);
  const second = secondary ? sparkPath(secondary, W, height, min, top) : null;
  return (
    <svg className={`spark ${className ?? ''}`} data-tone={tone} viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" height={height} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
      <defs>
        <linearGradient id={`g${id}`} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" style={{ stopColor: 'rgb(var(--c, var(--accent-rgb)))', stopOpacity: 0.32 }} />
          <stop offset="100%" style={{ stopColor: 'rgb(var(--c, var(--accent-rgb)))', stopOpacity: 0 }} />
        </linearGradient>
      </defs>
      {second ? <path d={second.line} className="spark__line spark__line--secondary" vectorEffect="non-scaling-stroke" /> : null}
      {main.area ? <path d={main.area} fill={`url(#g${id})`} /> : null}
      {main.line ? <path d={main.line} className="spark__line" vectorEffect="non-scaling-stroke" /> : null}
    </svg>
  );
}

/** Circular gauge (0..100). */
export function RingGauge({ value, size = 44, stroke = 4, tone = 'accent', children, label }: { value: number; size?: number; stroke?: number; tone?: Tone; children?: ReactNode; label?: string }) {
  const r = (size - stroke) / 2 - 1;
  const c = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(100, value || 0));
  return (
    <div className="ring" data-tone={tone} style={{ width: size, height: size }} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)} aria-label={label}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle className="ring__track" cx={size / 2} cy={size / 2} r={r} strokeWidth={stroke} />
        <circle
          className="ring__ticks"
          cx={size / 2}
          cy={size / 2}
          r={r + stroke / 2 + 2}
          strokeWidth={1}
          strokeDasharray={`1 ${((2 * Math.PI * (r + stroke / 2 + 2)) / 40 - 1).toFixed(2)}`}
        />
        <circle
          className="ring__value"
          cx={size / 2}
          cy={size / 2}
          r={r}
          strokeWidth={stroke}
          strokeDasharray={`${((pct / 100) * c).toFixed(2)} ${c.toFixed(2)}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <span className="ring__label">{children}</span>
    </div>
  );
}
