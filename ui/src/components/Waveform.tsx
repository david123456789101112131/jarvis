import { useEffect, useRef } from 'react';

interface WaveformProps {
  /** Read every frame: 0..1. */
  level: () => number;
  active: boolean;
  variant: 'band' | 'bars' | 'line';
  width: number;
  height: number;
  /** Changes when the theme changes so the stroke color is re-read from CSS. */
  colorKey?: string;
  className?: string;
  testId?: string;
}

function readColor(el: Element, name: string, fallback: string): string {
  const value = getComputedStyle(el).getPropertyValue(name).trim();
  return value || fallback;
}

/** Canvas 2D sound-wave visual driven by the voice level (band across the orb, bars, or idle line). */
export function Waveform({ level, active, variant, width, height, colorKey, className, testId }: WaveformProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  const levelRef = useRef(level);
  levelRef.current = level;

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const color = readColor(canvas, '--accent-2', '#59d0ff');
    const deep = readColor(canvas, '--accent', '#2ea8ff');
    const count = variant === 'band' ? Math.max(48, Math.round(width / 5)) : variant === 'bars' ? Math.max(24, Math.round(width / 6)) : 0;
    const phases = Array.from({ length: count }, (_, i) => Math.sin(i * 12.9898) * 43758.5453 % 6.283);
    let smooth = 0;
    let raf = 0;
    let t = 0;
    let last = performance.now();

    const draw = () => {
      ctx.clearRect(0, 0, width, height);
      const mid = height / 2;
      if (variant === 'line') {
        ctx.save();
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = color;
        ctx.shadowColor = deep;
        ctx.shadowBlur = 8;
        ctx.globalAlpha = 0.85;
        ctx.beginPath();
        for (let x = 0; x <= width; x += 2) {
          const u = x / width;
          const env = Math.sin(Math.PI * u);
          const y = mid + Math.sin(u * 22 + t * 3.1) * Math.sin(u * 5 - t * 1.3) * (1.5 + smooth * 10) * env;
          if (x === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.stroke();
        ctx.restore();
        return;
      }
      const gap = width / count;
      const barW = Math.max(1.2, gap * (variant === 'band' ? 0.42 : 0.5));
      ctx.save();
      ctx.shadowColor = deep;
      ctx.shadowBlur = variant === 'band' ? 10 : 6;
      const grad = ctx.createLinearGradient(0, 0, 0, height);
      grad.addColorStop(0, deep);
      grad.addColorStop(0.5, '#ffffff');
      grad.addColorStop(1, deep);
      ctx.fillStyle = variant === 'band' ? grad : color;
      for (let i = 0; i < count; i += 1) {
        const u = (i + 0.5) / count;
        const env = variant === 'band' ? Math.pow(Math.sin(Math.PI * u), 1.6) : Math.pow(Math.sin(Math.PI * u), 0.8);
        const wobble = 0.5 + 0.5 * Math.sin(t * (4.2 + (i % 7) * 0.6) + phases[i]);
        const wobble2 = 0.5 + 0.5 * Math.sin(t * 2.3 + i * 0.45);
        const amp = (0.08 + smooth * 0.92) * (0.35 + 0.65 * wobble * wobble2);
        const h = Math.max(1.5, amp * env * (height - 4));
        const x = i * gap + (gap - barW) / 2;
        ctx.globalAlpha = variant === 'band' ? 0.35 + 0.65 * env : 0.55 + 0.45 * env;
        ctx.fillRect(x, mid - h / 2, barW, h);
      }
      ctx.restore();
      if (variant === 'band') {
        ctx.save();
        ctx.globalAlpha = 0.5;
        const line = ctx.createLinearGradient(0, 0, width, 0);
        line.addColorStop(0, 'transparent');
        line.addColorStop(0.5, color);
        line.addColorStop(1, 'transparent');
        ctx.strokeStyle = line;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, mid);
        ctx.lineTo(width, mid);
        ctx.stroke();
        ctx.restore();
      }
    };

    const loop = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      t += dt;
      const raw = Math.max(0, Math.min(1, levelRef.current()));
      const k = raw > smooth ? 1 - Math.exp(-dt * 18) : 1 - Math.exp(-dt * 6);
      smooth += (raw - smooth) * k;
      draw();
      raf = requestAnimationFrame(loop);
    };

    if (active) raf = requestAnimationFrame(loop);
    else draw();
    return () => cancelAnimationFrame(raf);
  }, [active, variant, width, height, colorKey]);

  return <canvas ref={ref} className={className} data-testid={testId} style={{ width, height }} aria-hidden="true" />;
}
