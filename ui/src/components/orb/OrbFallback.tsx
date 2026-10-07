import type { CSSProperties } from 'react';
import type { OrbVisualState } from '../../state/selectors';

/** CSS-only orb used when WebGL is unavailable: same states, colors from the theme tokens. */
export function OrbFallback({ state, size, opacity = 1, className, style, testId }: { state: OrbVisualState; size: number; opacity?: number; className?: string; style?: CSSProperties; testId?: string }) {
  return (
    <div
      className={`orb orb-fallback orb-fallback--${state} ${className ?? ''}`}
      style={{ width: size, height: size, opacity, ...style }}
      data-testid={testId}
      data-orb-state={state}
      data-renderer="css"
      aria-hidden="true"
    >
      <div className="orb-fallback__halo" />
      <div className="orb-fallback__ring orb-fallback__ring--a" />
      <div className="orb-fallback__ring orb-fallback__ring--b" />
      <div className="orb-fallback__sphere">
        <div className="orb-fallback__grid" />
        <div className="orb-fallback__core" />
      </div>
    </div>
  );
}
