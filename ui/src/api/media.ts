import type { JarvisApi } from './client';
import type { Lang } from './types';
import type { Job } from './work';

// The "Image creation" and "Video creation" screens (core: `jarvis.media`, routes in `routes_media.py`).

export type MediaKind = 'image' | 'video';
export type VideoPart = 'scene' | 'characters' | 'motion' | 'lighting' | 'style';

export interface ImageModelOffer {
  id: string;
  label: string;
  /** Resolution → the price of one image made. */
  usd_per_image: Record<string, number>;
  /** Each image sent along (the one being changed). */
  usd_per_input: number;
}

export interface VideoModelOffer {
  id: string;
  label: string;
  /** Resolution → the price of one second. */
  usd_per_second: Record<string, number>;
  usd_per_input: number;
  /** Takes reference images. */
  references: boolean;
  resolutions: string[];
}

export interface StyleOffer {
  id: string;
  label: string;
  /** The words added to the description (what xAI receives). */
  fragment: string;
}

/** What the screens may choose from, with the prices shown before every Create (`catalog.offer`). */
export interface MediaOffer {
  image: {
    models: ImageModelOffer[];
    aspect_ratios: string[];
    resolutions: string[];
    max_images: number;
    styles: StyleOffer[];
    input_formats: string[];
    max_input_bytes: number;
  };
  video: {
    models: VideoModelOffer[];
    aspect_ratios: string[];
    resolutions: string[];
    reference_resolutions: string[];
    seconds: { min: number; max: number; default: number };
    max_references: number;
    parts: VideoPart[];
    /** The labels of the lines the text sent to xAI is made of. */
    part_labels: Record<VideoPart, string>;
    input_formats: string[];
    max_input_bytes: number;
    wait_minutes: number;
  };
  prices_checked: string;
  price_sources: string[];
  max_prompt: number;
}

export interface MediaStatus {
  /** xAI can be used: a key is saved, the network is on and api.x.ai is allowed. */
  available: boolean;
  xai: boolean;
  network: boolean;
  /** Only a demonstration can run. */
  demo: boolean;
  service: string;
  host: string;
  key_name: string;
  /** What is missing for real creation, in words. */
  missing: string[];
  reason: string | null;
  offer: MediaOffer;
  media_dir: string;
  counts: Record<MediaKind, number>;
}

/** One preview a screen made (`jarvis.media.store.MediaItem`). */
export interface MediaItem {
  id: string;
  kind: MediaKind;
  job_id: string;
  path: string;
  /** "PNG", "JPEG", "WEBP", "MP4", or "SVG" for a demonstration. */
  format: string;
  mime: string;
  bytes: number;
  width: number | null;
  height: number | null;
  seconds: number | null;
  /** False: a demonstration placeholder, never a real image or video. */
  real: boolean;
  description: string;
  /** Exactly what was sent to xAI. */
  prompt: string;
  params: Record<string, unknown>;
  /** What xAI reported it charged. */
  usd: number | null;
  /** The estimate shown before the request. */
  cost: string | null;
  parent: string | null;
  saved_to: string[];
  created_at: string;
  name: string;
  url: string;
}

export interface ImageRequest {
  description: string;
  style: string;
  model: string;
  count: number;
  aspect_ratio: string;
  resolution: string;
  source_item?: string | null;
  source_path?: string | null;
  demo: boolean;
  lang: Lang;
}

export interface RefineImageRequest {
  item_id: string;
  change: string;
  variation: boolean;
  count: number;
  lang: Lang;
}

export interface VideoRequest {
  scene: string;
  characters: string;
  motion: string;
  lighting: string;
  style: string;
  model: string;
  seconds: number;
  aspect_ratio: string;
  resolution: string;
  image_item?: string | null;
  image_path?: string | null;
  reference_items?: string[];
  reference_paths?: string[];
  demo: boolean;
  lang: Lang;
}

export interface RefineVideoRequest {
  item_id: string;
  change: string;
  lang: Lang;
}

export interface SaveRequest {
  item_id: string;
  /** The full path the user picked; the extension is set to the real format. */
  target: string;
  /** Only after the user said to replace an existing file (the core answers 409 "exists" first). */
  overwrite: boolean;
  lang: Lang;
}

const enc = encodeURIComponent;

/** Every action answers with the job it started; its progress then arrives as `job` events. */
export const media = {
  status: (api: JarvisApi, lang?: Lang) => api.request<MediaStatus>('GET', `/api/media/status${lang ? `?lang=${enc(lang)}` : ''}`),
  items: (api: JarvisApi, kind?: MediaKind) => api.request<MediaItem[]>('GET', `/api/media/items${kind ? `?kind=${enc(kind)}` : ''}`),
  createImage: (api: JarvisApi, body: ImageRequest) => api.request<Job>('POST', '/api/media/images', body),
  refineImage: (api: JarvisApi, body: RefineImageRequest) => api.request<Job>('POST', '/api/media/images/refine', body),
  createVideo: (api: JarvisApi, body: VideoRequest) => api.request<Job>('POST', '/api/media/videos', body),
  refineVideo: (api: JarvisApi, body: RefineVideoRequest) => api.request<Job>('POST', '/api/media/videos/refine', body),
  save: (api: JarvisApi, body: SaveRequest) => api.request<Job>('POST', '/api/media/save', body),
  remove: (api: JarvisApi, id: string) => api.request<{ deleted: boolean }>('DELETE', `/api/media/items/${enc(id)}`),
  clear: (api: JarvisApi, kind?: MediaKind) => api.request<{ cleared: number }>('DELETE', `/api/media/items${kind ? `?kind=${enc(kind)}` : ''}`),
};

// ---------------------------------------------------------------------------- the price and the text, as the core
// computes them (`jarvis.media.catalog`), so the screen shows them before anything is sent.

function priceOf(table: Record<string, number>, key: string): number {
  return table[key] ?? Math.max(0, ...Object.values(table));
}

/** Each image made, plus each image sent along (xAI bills inputs too). */
export function imageCost(offer: MediaOffer, modelId: string, resolution: string, count: number, inputs = 0): number {
  const model = offer.image.models.find((m) => m.id === modelId) ?? offer.image.models[0];
  if (!model) return 0;
  return priceOf(model.usd_per_image, resolution) * count + model.usd_per_input * inputs;
}

export function videoCost(offer: MediaOffer, modelId: string, resolution: string, seconds: number, inputs = 0): number {
  const model = offer.video.models.find((m) => m.id === modelId) ?? offer.video.models[0];
  if (!model) return 0;
  return priceOf(model.usd_per_second, resolution) * seconds + model.usd_per_input * inputs;
}

export function usd(amount: number): string {
  return amount > 0 && amount < 0.005 ? '<$0.01' : `$${amount.toFixed(2)}`;
}

/** The description as typed, then the style's words: exactly what goes to xAI. */
export function imagePrompt(offer: MediaOffer, description: string, styleId: string): string {
  const text = description.trim();
  const fragment = offer.image.styles.find((s) => s.id === styleId)?.fragment ?? '';
  return fragment ? `${text}\n\nStyle: ${fragment}` : text;
}

export function variationPrompt(original: string): string {
  return `A variation of this image: keep the subject, composition and style, change the details. Original description: ${original.trim()}`;
}

export function changePrompt(original: string, change: string): string {
  return `Change this image as asked, keep everything else as it is: ${change.trim()}\nOriginal description: ${original.trim()}`;
}

/** One labelled line for each part filled in, in the core's order. */
export function videoPrompt(offer: MediaOffer, parts: Record<VideoPart, string>): string {
  return offer.video.parts
    .filter((key) => parts[key].trim())
    .map((key) => `${offer.video.part_labels[key] ?? key}: ${parts[key].trim()}`)
    .join('\n');
}

export function videoChangePrompt(original: string, change: string): string {
  return `${original.trim()}\nChange: ${change.trim()}`;
}

/** The previews a finished create or change job made. */
export function jobItems(job: Job): MediaItem[] {
  const items = job.result?.items;
  return Array.isArray(items) ? (items as MediaItem[]) : [];
}

/** The extension a saved file gets (the core sets it to the file's real format). */
export const EXTENSION: Record<string, string> = { JPEG: 'jpg', PNG: 'png', WEBP: 'webp', MP4: 'mp4' };
