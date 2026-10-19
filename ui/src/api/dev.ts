import type { JarvisApi } from './client';
import type { Job } from './work';

/** The "Code & development" screen's API (core: `jarvis.server.routes_dev`, `jarvis.devmode`). */

export type EngineMode = 'auto' | 'fast' | 'strong';

/** An AI that could plan a change: a cloud one is used only on the user's click. */
export interface DevEngine {
  provider: string;
  name: string;
  model: string;
  local: boolean;
  /** USD per million tokens [in, out] by the list price; null when unknown (or free). */
  price: [number, number] | null;
  preferred: boolean;
}

export interface Bilingual {
  key?: string;
  he: string;
  en: string;
}

/** What JARVIS found about a project without running it or asking an AI (`jarvis.devmode.inspect`). */
export interface DevReport {
  root: string;
  name: string;
  files: number;
  truncated: boolean;
  scan_ms: number;
  languages: { name: string; files: number }[];
  formats: { name: string; files: number }[];
  manifests: string[];
  frameworks: string[];
  python: { present: boolean; dependencies: string[]; dev_dependencies: string[]; venv: boolean; venv_python: string; pytest_installed: boolean | null };
  node: { present: boolean; dependencies: string[]; dev_dependencies: string[]; manager: string; installed: boolean | null };
  commands: { test: string; run: string[]; build: string[]; check: string[] };
  tools: Record<string, boolean | string>;
  style: { indent: string; line_endings: string; bom: boolean; max_line: number | null; js_quotes?: string; js_semicolons?: boolean };
  readme: { path: string; text: string } | null;
  layout: { name: string; files: number }[];
  tests: { dirs: string[]; files: number; examples: string[] };
  secret_files: string[];
  missing: Bilingual[];
}

/** A project the user opened on the code screen. */
export interface DevSession {
  id: string;
  root: string;
  name: string;
  opened_at: string;
  /** "Allow changes in this project" is on (write access to this folder, for this session). */
  writable: boolean;
  report: DevReport | null;
  last_change: string | null;
  last_tests: string | null;
}

export interface DevStatus {
  available: boolean;
  engines: DevEngine[];
  needs: Bilingual[];
  prices_checked: string;
  mode: string;
  sessions: DevSession[];
  tools: { python: boolean; node: boolean; git: boolean };
  limits: { files: number; chars: number };
}

export interface RankedFile {
  path: string;
  score: number;
  size: number;
  reasons: string[];
  lines: number[];
}

/** One thing JARVIS found about a proposed file; `code` says what kind ("syntax", "import", "not_found"…). */
export interface DevNote {
  code: string;
  text: string;
}

export interface DevFile {
  path: string;
  action: 'modify' | 'create';
  why: string;
  include: boolean;
  edited: boolean;
  status: 'proposed' | 'applied' | 'undone' | 'conflict';
  added: number;
  removed: number;
  diff: string;
  /** The proposed text (secret-looking values hidden). */
  content: string;
  /** False when a hidden secret would be overwritten by editing here, or the file is already applied. */
  editable: boolean;
  masked: boolean;
  /** Has a problem: it cannot be applied until fixed or left out. */
  blocked: boolean;
  problems: DevNote[];
  warnings: DevNote[];
  eol: 'CRLF' | 'LF';
  new_file: boolean;
}

export type ChangeStatus = 'proposed' | 'applied' | 'partial' | 'undone' | 'discarded';

/** A proposed change as the screen reviews it. */
export interface DevChange {
  id: string;
  session_id: string;
  root: string;
  request: string;
  lang: string;
  created_at: string;
  understood: string;
  plan: string[];
  assumptions: string[];
  questions: string[];
  summary: string;
  risks: string[];
  tests: { added: boolean; command: string; notes: string };
  files: DevFile[];
  status: ChangeStatus;
  provider: string;
  model: string;
  effort: string;
  usage: Record<string, number>;
  cost_usd: number | null;
  /** The files whose text was sent to the AI. */
  sent: string[];
  /** How many secret-looking values were hidden before sending. */
  masked: number;
  parent_id: string | null;
  applied_at: string | null;
  undone_at: string | null;
  digest: string | null;
  undo_digest: string | null;
  can_apply: boolean;
  writable: boolean;
}

export interface DevChangeItem {
  id: string;
  request: string;
  summary: string;
  status: ChangeStatus;
  created_at: string;
  files: string[];
  can_undo: boolean;
  model: string;
  cost_usd: number | null;
}

/** The result of "Run tests": what ran, what passed and what failed. */
export interface TestsResult {
  summary: string;
  ok: boolean;
  runner: string;
  command: string;
  counts: Record<string, number>;
  failures: { name: string; message: string }[];
  exit_code: number | null;
  duration_s: number | null;
  timed_out: boolean;
  output: string;
  output_cut: boolean;
  hint: string;
}

export interface InspectResult {
  summary: string;
  report: DevReport;
  ranked: RankedFile[];
  listing: string[];
  listing_total: number;
  git: { ok: boolean; branch?: string; changed?: number; files?: string[]; error?: string } | null;
}

const enc = encodeURIComponent;
const session = (id: string) => `/api/dev/sessions/${enc(id)}`;
const change = (id: string) => `/api/dev/changes/${enc(id)}`;

export const dev = {
  status: (api: JarvisApi) => api.request<DevStatus>('GET', '/api/dev/status'),
  open: (api: JarvisApi, path: string, lang: string) => api.request<{ session: DevSession; job: Job }>('POST', '/api/dev/open', { path, lang }),
  allowChanges: (api: JarvisApi, id: string, allow: boolean, lang: string) => api.request<DevSession>('POST', `${session(id)}/write`, { allow, lang }),
  close: (api: JarvisApi, id: string) => api.request<{ closed: boolean }>('DELETE', session(id)),
  inspect: (api: JarvisApi, id: string, body: { request?: string; error_text?: string; refresh?: boolean; lang: string }) =>
    api.request<Job>('POST', `${session(id)}/inspect`, body),
  propose: (
    api: JarvisApi,
    id: string,
    body: { request: string; files: string[]; mode: EngineMode; error_text?: string; parent_id?: string; engine?: string; lang: string },
  ) => api.request<Job>('POST', `${session(id)}/propose`, body),
  changes: (api: JarvisApi, id: string) => api.request<DevChangeItem[]>('GET', `${session(id)}/changes`),
  change: (api: JarvisApi, id: string) => api.request<DevChange>('GET', change(id)),
  editFile: (api: JarvisApi, id: string, body: { path: string; content?: string; include?: boolean; lang: string }) =>
    api.request<DevChange>('PUT', `${change(id)}/files`, body),
  apply: (api: JarvisApi, id: string, lang: string) => api.request<Job>('POST', `${change(id)}/apply`, { lang }),
  undo: (api: JarvisApi, id: string, lang: string) => api.request<Job>('POST', `${change(id)}/undo`, { lang }),
  discard: (api: JarvisApi, id: string, lang: string) => api.request<DevChange>('POST', `${change(id)}/discard`, { lang }),
  tests: (api: JarvisApi, id: string, lang: string, command = '') => api.request<Job>('POST', `${session(id)}/tests`, { command, lang }),
  fix: (api: JarvisApi, id: string, body: { tests_job_id?: string; error_text?: string; request?: string; mode: EngineMode; engine?: string; lang: string }) =>
    api.request<Job>('POST', `${session(id)}/fix`, body),
};

// The core's estimate (`jarvis.devmode.prompt.estimate_cost`), so the price is visible before the click; the
// approval shows the core's own figure for exactly what is sent.
const CHARS_PER_TOKEN = 3.2;
const MAX_FILE_CHARS = 60_000;

/** [low, high] USD for one planning request, or null for a free or unpriced engine. */
export function estimateUsd(engine: DevEngine | undefined, fileBytes: number[], requestChars: number, mode: EngineMode): [number, number] | null {
  if (!engine || engine.local || !engine.price) return null;
  const chars = fileBytes.reduce((sum, size) => sum + Math.min(size, MAX_FILE_CHARS), 0) + requestChars + 6_000;
  const tokensIn = chars / CHARS_PER_TOKEN + 1_200;
  const [pin, pout] = engine.price;
  const low = (tokensIn * pin + (mode === 'strong' ? 4_000 : 1_500) * pout) / 1e6;
  const high = (tokensIn * pin + (mode === 'fast' ? 6_000 : 16_000) * pout) / 1e6;
  return [low, high];
}

export function money(usd: number): string {
  return usd < 0.1 ? `$${usd.toFixed(3)}` : `$${usd.toFixed(2)}`;
}

/** One line of a unified diff, typed for colouring. */
export type DiffLine = { kind: 'add' | 'del' | 'hunk' | 'meta' | 'ctx'; text: string };

export function diffLines(diff: string): DiffLine[] {
  return diff
    .split('\n')
    .filter((line, i, all) => line !== '' || i < all.length - 1)
    .map((text) => {
      if (text.startsWith('+++') || text.startsWith('---')) return { kind: 'meta', text };
      if (text.startsWith('@@')) return { kind: 'hunk', text };
      if (text.startsWith('+')) return { kind: 'add', text };
      if (text.startsWith('-')) return { kind: 'del', text };
      if (text.startsWith('\\')) return { kind: 'meta', text };
      return { kind: 'ctx', text };
    });
}
