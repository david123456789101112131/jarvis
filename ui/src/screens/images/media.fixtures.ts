// Test data of the image and video screens: the core's own offer (`catalog.offer("en")`, copied as it answers),
// a status, previews and jobs, and a stand-in for `api.request` that answers by route.

import type { MediaItem, MediaOffer, MediaStatus } from '../../api/media';
import type { Job } from '../../api/work';

export const OFFER: MediaOffer = {
  image: {
    models: [
      { id: 'grok-imagine-image', label: 'Standard', usd_per_image: { '1k': 0.02, '2k': 0.02 }, usd_per_input: 0.002 },
      { id: 'grok-imagine-image-quality', label: 'High quality', usd_per_image: { '1k': 0.05, '2k': 0.07 }, usd_per_input: 0.01 },
    ],
    aspect_ratios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3', '2:1', '1:2', '19.5:9', '9:19.5', '20:9', '9:20', 'auto'],
    resolutions: ['1k', '2k'],
    max_images: 4,
    styles: [
      { id: 'none', label: 'No particular style', fragment: '' },
      { id: 'photo', label: 'Realistic photo', fragment: 'photorealistic photograph, natural light, high detail' },
      { id: 'cinematic', label: 'Cinematic', fragment: 'cinematic film still, dramatic lighting, shallow depth of field' },
    ],
    input_formats: ['JPEG', 'PNG', 'WEBP'],
    max_input_bytes: 10000000,
  },
  video: {
    models: [
      { id: 'grok-imagine-video-1.5-lite', label: 'Lite (fast, low price)', usd_per_second: { '480p': 0.02, '720p': 0.03, '1080p': 0.14 }, usd_per_input: 0.01, references: false, resolutions: ['480p', '720p', '1080p'] },
      { id: 'grok-imagine-video-1.5', label: 'Full', usd_per_second: { '480p': 0.08, '720p': 0.14, '1080p': 0.25 }, usd_per_input: 0.01, references: true, resolutions: ['480p', '720p', '1080p'] },
    ],
    aspect_ratios: ['16:9', '9:16', '1:1', '4:3', '3:4', '3:2', '2:3'],
    resolutions: ['480p', '720p', '1080p'],
    reference_resolutions: ['480p', '720p'],
    seconds: { min: 1, max: 15, default: 6 },
    max_references: 3,
    parts: ['scene', 'characters', 'motion', 'lighting', 'style'],
    part_labels: { scene: 'Scene', characters: 'Characters', motion: 'Motion', lighting: 'Lighting', style: 'Style' },
    input_formats: ['JPEG', 'PNG', 'WEBP'],
    max_input_bytes: 10000000,
    wait_minutes: 15,
  },
  prices_checked: '2026-10-19',
  price_sources: ['https://docs.x.ai/developers/pricing'],
  max_prompt: 4000,
};

export function mediaStatus(available: boolean): MediaStatus {
  return {
    available,
    xai: available,
    network: true,
    demo: !available,
    service: 'Grok Imagine (xAI)',
    host: 'api.x.ai',
    key_name: 'XAI_API_KEY',
    missing: available ? [] : ['An xAI API key (XAI_API_KEY): Settings → API keys'],
    reason: available ? null : 'Creating real images and videos needs Grok Imagine (xAI), and it is not connected right now.',
    offer: OFFER,
    media_dir: 'C:\\Users\\me\\JARVIS\\workspace\\media',
    counts: { image: 0, video: 0 },
  };
}

export function mediaItem(extra: Partial<MediaItem> = {}): MediaItem {
  const id = extra.id ?? 'media_real1';
  return {
    id, kind: 'image', job_id: 'job_a', path: `C:\\Users\\me\\JARVIS\\workspace\\media\\job_a\\${id}.png`, format: 'PNG', mime: 'image/png',
    bytes: 2048, width: 1024, height: 1024, seconds: null, real: true, description: 'A fox', prompt: 'A fox', usd: 0.02, cost: '≈ $0.02',
    parent: null, saved_to: [], created_at: new Date().toISOString(), name: 'a-fox-1.png', url: `/api/media/file/${id}`,
    params: { model: 'grok-imagine-image', aspect_ratio: '1:1', resolution: '1k', style: 'none', changes: [] },
    ...extra,
  };
}

export function mediaJob(extra: Partial<Job> = {}): Job {
  return {
    id: 'job_m1', kind: 'image.create', area: 'images', title: 'Create one image: “A fox”', plan: ['Send the description to Grok Imagine (xAI)'],
    real: true, status: 'running', progress: null, message: '', result: null, error: null, sends: [], cost: null,
    created_at: new Date().toISOString(), finished_at: null, ms: null, can_retry: false, retry_of: null, params: {}, lang: 'en',
    keep_result: true, steps: [{ key: 'prepare', label: 'Preparing the request', status: 'running', detail: '', ms: null }],
    ...extra,
  };
}

export type Handler = (body: unknown, path: string) => unknown;

/** `api.request` answering `"METHOD /path"` (the path without its query); anything else is a 404. */
export function routes(table: Record<string, Handler>) {
  return async (method: string, path: string, body?: unknown) => {
    const handler = table[`${method} ${path.split('?')[0]}`];
    if (!handler) throw new Error(`no route for ${method} ${path}`);
    return handler(body, path);
  };
}
