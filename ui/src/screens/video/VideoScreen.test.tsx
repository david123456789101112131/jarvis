import { cleanup, fireEvent, waitFor, within } from '@testing-library/react';
import type { Mock } from 'vitest';
import { afterEach, describe, expect, it } from 'vitest';
import { ApiError } from '../../api/client';
import { renderWith } from '../../test/render';
import { mediaItem, mediaJob, mediaStatus, routes } from '../images/media.fixtures';
import { VideoScreen } from './VideoScreen';

afterEach(cleanup);

function sent(request: Mock, method: string, path: string): unknown[] {
  return request.mock.calls.filter((c) => c[0] === method && c[1] === path).map((c) => c[2]);
}

function pick(group: HTMLElement, value: string) {
  const option = group.querySelector<HTMLElement>(`[data-value="${value}"]`);
  if (!option) throw new Error(`no option ${value}`);
  fireEvent.click(option);
}

const VIDEO = mediaItem({
  id: 'media_vid1', kind: 'video', format: 'MP4', mime: 'video/mp4', width: null, height: null, seconds: 6, name: 'a-beach.mp4',
  description: 'A beach', prompt: 'Scene: A beach', usd: 0.12, cost: '≈ $0.12',
  params: { model: 'grok-imagine-video-1.5-lite', resolution: '480p', seconds: 6, aspect_ratio: '16:9', parts: { scene: 'A beach' }, image: null, references: [] },
});
const STORYBOARD = mediaItem({ id: 'media_sb1', kind: 'video', real: false, format: 'SVG', mime: 'image/svg+xml', width: null, height: null, name: 'storyboard.svg', usd: null, cost: null });

describe('the video screen', () => {
  it('in the simulation it says so and offers nothing to create', async () => {
    const { findByTestId, queryByTestId, request } = renderWith(<VideoScreen />, { mock: true });
    expect((await findByTestId('video-unavailable')).textContent).toContain('needs the real JARVIS core');
    expect(queryByTestId('video-create')).toBeNull();
    expect(queryByTestId('video-demo')).toBeNull();
    expect(request).not.toHaveBeenCalled();
  });

  it('without xAI there is no Create button at all, only a demonstration that says so (in Hebrew)', async () => {
    const { findByTestId, getByTestId, queryByTestId, container, request } = renderWith(<VideoScreen />, {
      lang: 'he',
      request: routes({
        'GET /api/media/status': () => mediaStatus(false),
        'GET /api/media/items': () => [],
        'POST /api/media/videos': () => mediaJob({ kind: 'video.create', area: 'video', real: false }),
      }),
    });
    const unavailable = await findByTestId('video-unavailable');
    expect(unavailable.textContent).toContain('XAI_API_KEY');
    expect(unavailable.textContent).toContain('אין כאן כפתור יצירה');
    expect(queryByTestId('video-create')).toBeNull();
    expect([...container.querySelectorAll('button')].some((b) => (b.textContent ?? '').includes('ליצור את הסרטון'))).toBe(false);
    expect(queryByTestId('video-start')).toBeNull(); // nothing of the user's to pick: nothing is sent
    expect(getByTestId('video-sends').textContent).toContain('הדגמה — לא נוצר סרטון אמיתי');

    fireEvent.change(getByTestId('video-part-scene'), { target: { value: 'חוף' } });
    fireEvent.click(getByTestId('video-demo'));
    expect(getByTestId('video-demo').textContent).toBe('לעבור על השלבים כהדגמה (לא נוצר סרטון)');
    await waitFor(() => expect(sent(request, 'POST', '/api/media/videos')).toHaveLength(1));
    expect(sent(request, 'POST', '/api/media/videos')[0]).toEqual({
      scene: 'חוף', characters: '', motion: '', lighting: '', style: '', model: 'grok-imagine-video-1.5-lite', seconds: 6, aspect_ratio: '16:9',
      resolution: '480p', image_item: null, image_path: null, reference_items: [], reference_paths: [], demo: true, lang: 'he',
    });
  });

  it('with xAI it shows the exact text, the images sent, the price and the stop note before Create', async () => {
    const dialog = ['C:\\pics\\a.png'];
    const { findByTestId, getByTestId, container, request } = renderWith(<VideoScreen />, {
      request: routes({
        'GET /api/media/status': () => mediaStatus(true),
        'GET /api/media/items': (_body, path) => (path.includes('kind=image') ? [mediaItem({ id: 'media_pic1', name: 'fox.png' })] : []),
        'POST /api/dialog': () => {
          const path = dialog.shift();
          if (!path) throw new ApiError(409, 'conflict', 'no window'); // then the path is typed
          return { paths: [path] };
        },
        'POST /api/media/videos': () => mediaJob({ kind: 'video.create', area: 'video' }),
      }),
    });
    fireEvent.change(await findByTestId('video-part-scene'), { target: { value: 'A beach at dawn' } });
    fireEvent.change(getByTestId('video-part-motion'), { target: { value: 'The camera pans left' } });
    fireEvent.change(getByTestId('video-part-style'), { target: { value: 'Cinematic' } });
    expect(getByTestId('video-sends-text').textContent).toBe('Scene: A beach at dawn\nMotion: The camera pans left\nStyle: Cinematic');
    expect(getByTestId('video-sends-price').textContent).toBe('Estimated price: $0.12');
    expect(getByTestId('video-sends').textContent).toContain('xAI may still finish the video on its side and charge for it');
    expect(getByTestId('video-sends').textContent).toContain('up to 15 minutes');

    fireEvent.change(getByTestId('video-seconds'), { target: { value: '8' } });
    expect(getByTestId('video-seconds-value').textContent).toBe('8 seconds');
    pick(getByTestId('video-start'), 'image');
    fireEvent.change(await findByTestId('video-image-preview'), { target: { value: 'media_pic1' } });
    expect(getByTestId('video-sends-file').textContent).toContain('the preview fox.png');
    expect(getByTestId('video-sends-price').textContent).toBe('Estimated price: $0.17');
    expect((getByTestId('video-aspect') as HTMLSelectElement).disabled).toBe(true); // the video takes the picture's shape

    // Reference images: the Full model is chosen for them, and 1080p is not offered.
    pick(getByTestId('video-start'), 'references');
    expect(getByTestId('video-note').textContent).toBe('Reference images work only with the Full model, so it was chosen.');
    expect(getByTestId('video-model').querySelector('[aria-checked="true"]')?.getAttribute('data-value')).toBe('grok-imagine-video-1.5');
    expect([...getByTestId('video-resolution').querySelectorAll('[data-value]')].map((o) => o.getAttribute('data-value'))).toEqual(['480p', '720p']);
    fireEvent.click(getByTestId('video-add-reference-open'));
    await waitFor(() => expect(getByTestId('video-references').children).toHaveLength(1));
    fireEvent.click(getByTestId('video-add-reference-open'));
    fireEvent.change(await findByTestId('video-add-reference-typed'), { target: { value: 'C:\\pics\\b.png' } });
    expect(container.querySelector('form form')).toBeNull(); // the picker's own form is never nested in another
    fireEvent.click(getByTestId('video-add-reference-use'));
    await waitFor(() => expect(getByTestId('video-references').children).toHaveLength(2));
    pick(getByTestId('video-resolution'), '720p');
    expect(getByTestId('video-sends').textContent).toContain('your file b.png');
    expect(getByTestId('video-create').textContent).toBe('Create the video · $1.14');

    fireEvent.click(getByTestId('video-create'));
    await waitFor(() => expect(sent(request, 'POST', '/api/media/videos')).toHaveLength(1));
    expect(sent(request, 'POST', '/api/media/videos')[0]).toEqual({
      scene: 'A beach at dawn', characters: '', motion: 'The camera pans left', lighting: '', style: 'Cinematic', model: 'grok-imagine-video-1.5', seconds: 8,
      aspect_ratio: '16:9', resolution: '720p', image_item: null, image_path: null, reference_items: [], reference_paths: ['C:\\pics\\a.png', 'C:\\pics\\b.png'],
      demo: false, lang: 'en',
    });
  });

  it('plays a real clip and saves it as MP4; a storyboard is marked as a demonstration and cannot be saved', async () => {
    const { findAllByTestId, findByTestId, getByTestId } = renderWith(<VideoScreen />, {
      request: routes({ 'GET /api/media/status': () => mediaStatus(true), 'GET /api/media/items': () => [VIDEO, STORYBOARD] }),
    });
    const [clip, board] = await findAllByTestId('media-item');
    expect(within(clip).getByTestId('media-video')).toBeTruthy();
    expect(within(clip).queryByTestId('media-demo-badge')).toBeNull();
    expect(within(board).getByTestId('media-demo-badge').textContent).toBe('Demonstration — no real video was created');

    fireEvent.click(within(clip).getByTestId('media-item-open'));
    const detail = await findByTestId('media-selected');
    expect((detail.querySelector('video') as HTMLVideoElement).controls).toBe(true);
    expect(getByTestId('media-save-pick-open').textContent).toBe('Save as MP4…');
    expect(getByTestId('media-save').textContent).toContain('so the name ends in .mp4');
    expect(getByTestId('media-facts').textContent).toContain('xAI charged $0.12');

    fireEvent.click(within(board).getByTestId('media-item-open'));
    expect(((await findByTestId('media-save-disabled')) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(getByTestId('video-change'), { target: { value: 'Make it night' } });
    expect(getByTestId('video-refine').textContent).toBe('Demonstrate the change');
    expect(getByTestId('video-refine-sends').textContent).toContain('nothing is sent');
  });

  it('changes a clip with the text and the price shown first, and deletes it after a second click', async () => {
    let kept = [VIDEO];
    const { findAllByTestId, findByTestId, getByTestId, queryByTestId, request } = renderWith(<VideoScreen />, {
      request: routes({
        'GET /api/media/status': () => mediaStatus(true),
        'GET /api/media/items': () => kept,
        'POST /api/media/videos/refine': () => mediaJob({ id: 'job_vr', kind: 'video.refine', area: 'video' }),
        'DELETE /api/media/items/media_vid1': () => {
          kept = [];
          return { deleted: true };
        },
      }),
    });
    fireEvent.click(within((await findAllByTestId('media-item'))[0]).getByTestId('media-item-open'));
    fireEvent.change(await findByTestId('video-change'), { target: { value: 'Make it night' } });
    expect(getByTestId('video-refine-sends-text').textContent).toBe('Scene: A beach\nChange: Make it night');
    expect(getByTestId('video-refine-sends-price').textContent).toBe('Estimated price: $0.12');
    fireEvent.click(getByTestId('video-refine'));
    await waitFor(() => expect(sent(request, 'POST', '/api/media/videos/refine')).toHaveLength(1));
    expect(sent(request, 'POST', '/api/media/videos/refine')[0]).toEqual({ item_id: 'media_vid1', change: 'Make it night', lang: 'en' });
    expect((await findAllByTestId('job-card'))[0].getAttribute('data-kind')).toBe('video.refine');

    fireEvent.click(getByTestId('media-delete'));
    fireEvent.click(getByTestId('media-delete'));
    await waitFor(() => expect(queryByTestId('media-selected')).toBeNull());
    expect(sent(request, 'DELETE', '/api/media/items/media_vid1')).toHaveLength(1);
    expect(await findByTestId('media-empty')).toBeTruthy();
  });
});
