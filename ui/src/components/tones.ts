import type { AgentStatus, RiskLevel, TaskStatus, VoiceState } from '../api/types';

export type Tone = 'muted' | 'steel' | 'think' | 'work' | 'warn' | 'ok' | 'danger' | 'orange' | 'verify' | 'info' | 'accent';

export const AGENT_TONE: Record<AgentStatus, Tone> = {
  offline: 'muted',
  idle: 'steel',
  thinking: 'think',
  working: 'work',
  waiting: 'warn',
  completed: 'ok',
  error: 'danger',
};

export const TASK_TONE: Record<TaskStatus, Tone> = {
  pending: 'muted',
  planning: 'think',
  queued: 'steel',
  running: 'work',
  waiting: 'warn',
  verifying: 'verify',
  completed: 'ok',
  failed: 'danger',
  cancelled: 'muted',
  interrupted: 'orange',
  skipped: 'muted',
};

export const RISK_TONE: Record<RiskLevel, Tone> = {
  safe: 'ok',
  low: 'info',
  medium: 'warn',
  high: 'orange',
  critical: 'danger',
};

export function riskTone(risk: string): Tone {
  return RISK_TONE[(risk?.toLowerCase() as RiskLevel) ?? 'low'] ?? 'steel';
}

export const DECISION_TONE: Record<string, Tone> = {
  allowed: 'ok',
  confirmed: 'info',
  denied: 'warn',
  rejected: 'warn',
  blocked: 'danger',
  error: 'danger',
};

export const OUTCOME_TONE: Record<string, Tone> = {
  ok: 'ok',
  pending: 'steel',
  error: 'danger',
  skipped: 'muted',
};

export const LEVEL_TONE: Record<string, Tone> = {
  debug: 'muted',
  info: 'steel',
  success: 'ok',
  warning: 'warn',
  error: 'danger',
};

export const VOICE_TONE: Record<VoiceState | 'busy', Tone> = {
  disabled: 'muted',
  idle: 'accent',
  wake: 'info',
  listening: 'work',
  processing: 'think',
  speaking: 'ok',
  error: 'danger',
  busy: 'work',
};

/** Statuses that animate (pulse) their dot. */
export const LIVE_STATUSES = new Set<string>(['thinking', 'working', 'planning', 'running', 'verifying', 'listening', 'processing', 'speaking', 'wake']);
