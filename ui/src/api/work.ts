import type { JarvisApi } from './client';

/** The screen a job belongs to. */
export type JobArea = 'code' | 'images' | 'video' | 'apps';
export type JobStatus = 'running' | 'done' | 'failed' | 'cancelled';
export type JobStepStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped';

export interface JobStep {
  key: string;
  label: string;
  status: JobStepStatus;
  detail: string;
  ms: number | null;
}

/** One action a work screen started (core: `jarvis.jobs.Job`), live over the `job` event. */
export interface Job {
  id: string;
  kind: string;
  area: JobArea | string;
  /** What JARVIS understood it was asked to do. */
  title: string;
  /** What it is about to do. */
  plan: string[];
  /** A real action; false means a demonstration (and the UI says so). */
  real: boolean;
  status: JobStatus;
  steps: JobStep[];
  progress: number | null;
  message: string;
  result: Record<string, unknown> | null;
  error: string | null;
  /** What leaves the computer, and where to. Empty: nothing does. */
  sends: string[];
  cost: string | null;
  created_at: string;
  finished_at: string | null;
  ms: number | null;
  can_retry: boolean;
  retry_of: string | null;
  params: Record<string, unknown>;
  lang: string;
  keep_result: boolean;
}

export type DialogKind = 'open' | 'folder' | 'save';

export interface DialogOptions {
  kind: DialogKind;
  title?: string;
  directory?: string;
  save_name?: string;
  /** pywebview's format: `'Images (*.png;*.jpg)'`. */
  file_types?: string[];
  multiple?: boolean;
}

export interface MetricSummary {
  count: number;
  ok: number;
  median_ms: number;
  p90_ms: number;
  min_ms: number;
  max_ms: number;
  last_ms: number;
  last_at: string;
}

export interface MetricsView {
  summary: Record<string, MetricSummary>;
  snapshots: Record<string, { at: string; summary: Record<string, MetricSummary> }>;
}

const enc = encodeURIComponent;

export const work = {
  jobs: (api: JarvisApi, area?: JobArea, limit = 50) =>
    api.request<Job[]>('GET', `/api/jobs?limit=${limit}${area ? `&area=${enc(area)}` : ''}`),
  job: (api: JarvisApi, id: string) => api.request<Job>('GET', `/api/jobs/${enc(id)}`),
  cancel: (api: JarvisApi, id: string) => api.request<Job>('POST', `/api/jobs/${enc(id)}/cancel`, {}),
  retry: (api: JarvisApi, id: string) => api.request<Job>('POST', `/api/jobs/${enc(id)}/retry`, {}),
  clear: (api: JarvisApi) => api.request<{ cleared: number }>('DELETE', '/api/jobs'),
  /** The window's native dialog; `null` when the user cancelled. Throws ApiError 409 when there is no window. */
  dialog: async (api: JarvisApi, options: DialogOptions) => (await api.request<{ paths: string[] | null }>('POST', '/api/dialog', options)).paths,
  metrics: (api: JarvisApi, prefix?: string) => api.request<MetricsView>('GET', `/api/metrics${prefix ? `?prefix=${enc(prefix)}` : ''}`),
  snapshot: (api: JarvisApi, label: string) => api.request<{ at: string }>('POST', '/api/metrics/snapshot', { label }),
  clearMetrics: (api: JarvisApi) => api.request<{ ok: boolean }>('DELETE', '/api/metrics'),
};

/** A finished job's duration, or how long it has been running. */
export function jobSeconds(job: Job, now = Date.now()): number {
  if (job.ms !== null && job.ms !== undefined) return job.ms / 1000;
  const started = Date.parse(job.created_at);
  return Number.isFinite(started) ? Math.max(0, (now - started) / 1000) : 0;
}
