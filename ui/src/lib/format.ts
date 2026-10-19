import type { Lang } from '../api/types';

export function pct(value: number | null | undefined): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 0;
  return Math.round(Math.max(0, Math.min(100, value)));
}

/** 0..1 progress → 0..100 integer. */
export function progressPct(value: number | null | undefined): number {
  return pct((value ?? 0) * 100);
}

export function formatRate(kbps: number): string {
  if (!Number.isFinite(kbps) || kbps < 0) return '0 KB/s';
  if (kbps >= 1024 * 1024) return `${(kbps / 1024 / 1024).toFixed(1)} GB/s`;
  if (kbps >= 1024) return `${(kbps / 1024).toFixed(kbps >= 10240 ? 0 : 1)} MB/s`;
  return `${Math.round(kbps)} KB/s`;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`;
}

export function formatGb(gb: number): string {
  if (!Number.isFinite(gb)) return '—';
  return gb >= 1000 ? `${(gb / 1024).toFixed(1)} TB` : `${gb.toFixed(gb >= 100 ? 0 : 1)} GB`;
}

const UNITS: Record<Lang, { d: string; h: string; m: string; s: string }> = {
  en: { d: 'd', h: 'h', m: 'm', s: 's' },
  he: { d: ' ימ׳', h: ' שע׳', m: ' ד׳', s: ' שנ׳' },
};

/** Compact duration: "2h 05m", "3m 12s", "45s". */
export function formatDuration(seconds: number | null | undefined, lang: Lang): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '—';
  const u = UNITS[lang];
  const s = Math.round(seconds);
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (days > 0) return `${days}${u.d} ${hours}${u.h}`;
  if (hours > 0) return `${hours}${u.h} ${String(minutes).padStart(2, '0')}${u.m}`;
  if (minutes > 0) return `${minutes}${u.m} ${String(secs).padStart(2, '0')}${u.s}`;
  return `${secs}${u.s}`;
}

/** A measured time with the precision it deserves: "0.35s", "4.2s", "3m 12s" (Hebrew: "0.35 שנ׳"). */
export function formatMs(ms: number | null | undefined, lang: Lang): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return '—';
  const seconds = ms / 1000;
  if (seconds >= 60) return formatDuration(seconds, lang);
  const digits = seconds < 1 ? 2 : seconds < 10 ? 1 : 0;
  return `${seconds.toFixed(digits)}${UNITS[lang].s}`;
}

export function durationBetween(start: string | null | undefined, end: string | null | undefined, now = Date.now()): number | null {
  if (!start) return null;
  const a = Date.parse(start);
  const b = end ? Date.parse(end) : now;
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  return Math.max(0, (b - a) / 1000);
}

const relCache = new Map<Lang, Intl.RelativeTimeFormat>();

export function formatRelative(iso: string | null | undefined, lang: Lang, now = Date.now()): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '—';
  let rtf = relCache.get(lang);
  if (!rtf) {
    rtf = new Intl.RelativeTimeFormat(lang === 'he' ? 'he' : 'en', { numeric: 'auto', style: 'short' });
    relCache.set(lang, rtf);
  }
  const diff = (t - now) / 1000;
  const abs = Math.abs(diff);
  if (abs < 45) return rtf.format(0, 'second');
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour');
  return rtf.format(Math.round(diff / 86400), 'day');
}

export function formatClock(date: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(date.getHours())}:${p(date.getMinutes())}:${p(date.getSeconds())}`;
}

export function formatTimeOf(iso: string | null | undefined): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  return Number.isFinite(t) ? formatClock(new Date(t)) : '—';
}

export function formatDateTime(iso: string | null | undefined, lang: Lang): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '—';
  return new Intl.DateTimeFormat(lang === 'he' ? 'he-IL' : 'en-GB', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(t));
}

const HEBREW = /[֐-׿]/;

export function hasHebrew(text: string): boolean {
  return HEBREW.test(text);
}

/** Text direction for a piece of user content. */
export function textDir(text: string, language?: string | null): 'rtl' | 'ltr' {
  if (language === 'he') return 'rtl';
  if (language === 'en') return 'ltr';
  return hasHebrew(text) ? 'rtl' : 'ltr';
}

export function fileName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}
