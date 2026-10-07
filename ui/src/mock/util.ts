let counter = 0;

export function mockId(prefix: string): string {
  counter += 1;
  return `${prefix}_${Date.now().toString(36).slice(-5)}${counter.toString(36)}${Math.random().toString(36).slice(2, 5)}`;
}

export function isoAt(ms: number): string {
  return new Date(ms).toISOString();
}

export function hmsAt(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function clone<T>(value: T): T {
  return value === undefined ? value : (JSON.parse(JSON.stringify(value)) as T);
}

export function rand(min: number, max: number): number {
  return min + Math.random() * (max - min);
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function unique<T>(items: T[]): T[] {
  return [...new Set(items)];
}

export function hasHebrew(text: string): boolean {
  return /[֐-׿]/.test(text);
}
