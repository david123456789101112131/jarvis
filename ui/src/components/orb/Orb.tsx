import { useEffect, useRef, useState, type CSSProperties } from 'react';
import type { Theme } from '../../api/types';
import type { OrbVisualState } from '../../state/selectors';
import { OrbFallback } from './OrbFallback';
import { OrbEngine, webglAvailable } from './OrbEngine';

export interface OrbProps {
  state: OrbVisualState;
  theme: Theme;
  /** CSS pixel size of the (square) orb canvas. */
  size: number;
  opacity?: number;
  followMouse?: boolean;
  /** Read once per frame: mic/speech level 0..1. */
  level?: () => number;
  quality?: 'high' | 'low';
  className?: string;
  style?: CSSProperties;
  testId?: string;
}

function forcedFallback(): boolean {
  try {
    return new URLSearchParams(window.location.search).get('gl') === '0';
  } catch {
    return false;
  }
}

/** The JARVIS core orb: Three.js when WebGL works, a CSS rendition otherwise. */
export function Orb({ state, theme, size, opacity = 1, followMouse = false, level, quality = 'high', className, style, testId }: OrbProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<OrbEngine | null>(null);
  const [fallback, setFallback] = useState(() => forcedFallback() || !webglAvailable());
  const initial = useRef({ state, theme, followMouse, level });

  useEffect(() => {
    if (fallback || !canvasRef.current) return;
    let engine: OrbEngine;
    try {
      const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
      engine = new OrbEngine(canvasRef.current, { theme: initial.current.theme, quality, reducedMotion: reduced, onContextLost: () => setFallback(true) });
    } catch (err) {
      console.warn('JARVIS orb: WebGL unavailable, using the CSS fallback.', err);
      setFallback(true);
      return;
    }
    engineRef.current = engine;
    engine.setState(initial.current.state);
    engine.setFollowMouse(initial.current.followMouse);
    engine.setLevelSource(initial.current.level ?? null);
    const canvas = canvasRef.current;
    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      engine.resize(Math.max(2, Math.round(rect.width)), Math.max(2, Math.round(rect.height)), window.devicePixelRatio || 1);
    };
    resize();
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    observer?.observe(canvas);
    window.addEventListener('resize', resize);
    if (!document.hidden) engine.start();
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', resize);
      engine.dispose();
      engineRef.current = null;
    };
  }, [fallback, quality]);

  useEffect(() => {
    engineRef.current?.setState(state);
  }, [state]);
  useEffect(() => {
    engineRef.current?.setTheme(theme);
  }, [theme]);
  useEffect(() => {
    engineRef.current?.setLevelSource(level ?? null);
  }, [level]);
  useEffect(() => {
    const engine = engineRef.current;
    engine?.setFollowMouse(followMouse);
    if (!followMouse || !engine) return;
    const onMove = (e: PointerEvent) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const reach = Math.max(rect.width, Math.min(window.innerWidth, window.innerHeight) * 0.6);
      engine.setPointer((e.clientX - cx) / reach, -(e.clientY - cy) / reach);
    };
    window.addEventListener('pointermove', onMove, { passive: true });
    return () => window.removeEventListener('pointermove', onMove);
  }, [followMouse, fallback]);

  const boxStyle: CSSProperties = { width: size, height: size, opacity, ...style };
  if (fallback) return <OrbFallback state={state} size={size} opacity={opacity} className={className} style={style} testId={testId} />;
  return (
    <div className={`orb ${className ?? ''}`} style={boxStyle} data-testid={testId} data-orb-state={state} data-renderer="webgl">
      <canvas ref={canvasRef} className="orb__canvas" aria-hidden="true" />
    </div>
  );
}
