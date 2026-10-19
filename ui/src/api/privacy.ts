import type { JarvisApi } from './client';

/** A kind of data JARVIS keeps that the privacy screen can delete (core: `jarvis.privacy.CLEARABLE`). */
export type PrivacyKey =
  | 'chat'
  | 'commands'
  | 'tasks'
  | 'memory'
  | 'projects'
  | 'files'
  | 'jobs'
  | 'code'
  | 'media'
  | 'speed'
  | 'logs'
  | 'temp'
  | 'approvals';

/** One kind of saved data: how much, and where. Never the data itself. */
export interface PrivacyItem {
  key: PrivacyKey;
  count: number;
  /** The size on disk, when it is in files of its own (null: it shares the memory database). */
  bytes: number | null;
  where: string[];
  can_clear: boolean;
  /** Work of that screen is running now; deleting waits until it is done. */
  busy: boolean;
  /** tasks: still in progress or interrupted (they stay). */
  kept?: number;
  /** jobs: running now (they stay). */
  running?: number;
  /** code: projects whose scan is cached. */
  cached?: number;
}

/** What this screen does not delete, on purpose. */
export interface PrivacyKept {
  key: 'keys' | 'settings' | 'work' | 'models' | string;
  where: string[];
  /** keys: how many are saved, and their names (never their values). */
  count?: number;
  names?: string[];
}

/** What can reach the microphone, the screen and the internet, as JARVIS is set up right now. */
export interface PrivacySenses {
  microphone: {
    enabled: boolean;
    state: string;
    muted: boolean;
    wake_word: string;
    always_listening: boolean;
    ctrl_to_talk: boolean;
    stt: string;
    /** Speech recognition sends the recorded command out (OpenAI). */
    stt_online: boolean;
    tts: string;
    /** Speech sends the reply's text out (Microsoft's online voice). */
    tts_online: boolean;
  };
  screen: { computer_enabled: boolean; model: 'local' | 'remote' | string; remote_host: string | null };
  cloud: { only_when_told: boolean; ai: { name: string; configured: boolean }[] };
}

export interface PrivacyInventory {
  items: PrivacyItem[];
  database: { where: string; bytes: number };
  kept: PrivacyKept[];
  senses: PrivacySenses;
}

export interface PrivacyResult {
  ok: boolean;
  count?: number;
  reason?: 'busy' | 'error' | string;
  /** logs: files a running part of JARVIS writes right now (emptied next time). */
  in_use?: string[];
  kept?: number;
}

export interface PrivacyCleared {
  results: Partial<Record<PrivacyKey, PrivacyResult>>;
  inventory: PrivacyInventory;
}

export const privacy = {
  inventory: (api: JarvisApi) => api.request<PrivacyInventory>('GET', '/api/privacy'),
  clear: (api: JarvisApi, items: PrivacyKey[]) => api.request<PrivacyCleared>('POST', '/api/privacy/clear', { items }),
};
