import type { Theme } from '../../api/types';

export interface OrbPalette {
  base: string;
  hot: string;
  deep: string;
}

/** Orb colors per theme; the CSS accents in tokens.css use the same hues. */
export const ORB_PALETTES: Record<Theme, OrbPalette> = {
  blue: { base: '#2ea8ff', hot: '#d4f3ff', deep: '#0a3a8a' },
  green: { base: '#1fe08f', hot: '#d2ffec', deep: '#06573a' },
  orange: { base: '#ff9a2e', hot: '#ffe6c4', deep: '#7a3606' },
  red: { base: '#ff3b4f', hot: '#ffd3d8', deep: '#6b0a16' },
  purple: { base: '#a35bff', hot: '#efe2ff', deep: '#3a1374' },
  white: { base: '#c9d8ee', hot: '#ffffff', deep: '#3d4b62' },
};

export const THINK_PALETTE: OrbPalette = { base: '#9b5cff', hot: '#ecdcff', deep: '#341070' };
export const ERROR_PALETTE: OrbPalette = ORB_PALETTES.red;

export type RGB = [number, number, number];

export function hexToRgb(hex: string): RGB {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

export function mixRgb(a: RGB, b: RGB, t: number): RGB {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

export function desaturate(c: RGB, amount: number, dim: number): RGB {
  const l = c[0] * 0.3 + c[1] * 0.59 + c[2] * 0.11;
  const g = mixRgb(c, [l, l, l], amount);
  return [g[0] * (1 - dim), g[1] * (1 - dim), g[2] * (1 - dim)];
}
