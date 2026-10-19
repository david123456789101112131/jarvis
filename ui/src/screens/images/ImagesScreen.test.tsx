import { cleanup, fireEvent, waitFor, within } from '@testing-library/react';
import type { Mock } from 'vitest';
import { afterEach, describe, expect, it } from 'vitest';
import { ApiError } from '../../api/client';
import { renderWith } from '../../test/render';
import { ImagesScreen } from './ImagesScreen';
import { mediaItem, mediaJob, mediaStatus, routes } from './media.fixtures';

afterEach(cleanup);

/** The bodies sent to one route, in order. */
function sent(request: Mock, method: string, path: string): unknown[] {
  return request.mock.calls.filter((c) => c[0] === method && c[1] === path).map((c) => c[2]);
}

function pick(group: HTMLElement, value: string) {
  const option = group.querySelector<HTMLElement>(`[data-value="${value}"]`);
  if (!option) throw new Error(`no option ${value}`);
  fireEvent.click(option);
}

describe('the image screen', () => {
  it('in the simulation it says so and offers nothing to create', async () => {
    const { findByTestId, queryByTestId, request } = renderWith(<ImagesScreen />, { mock: true });
    expect((await findByTestId('images-unavailable')).textContent).toContain('needs the real JARVIS core');
    expect(queryByTestId('images-create')).toBeNull();
    expect(queryByTestId('images-demo')).toBeNull();
    expect(request).not.toHaveBeenCalled();
  });

  it('without xAI it says what is missing and offers only a marked demonstration (in Hebrew)', async () => {
    const { findByTestId, getByTestId, queryByTestId, findAllByTestId, request } = renderWith(<ImagesScreen />, {
      lang: 'he',
      request: routes({
        'GET /api/media/status': () => mediaStatus(false),
        'GET /api/media/items': () => [],
        'POST /api/media/images': () => mediaJob({ real: false, title: 'ליצור תמונה אחת: «שועל»' }),
      }),
    });
    expect((await findByTestId('images-unavailable')).textContent).toContain('XAI_API_KEY');
    expect(queryByTestId('images-create')).toBeNull();
    expect(getByTestId('images-sends').textContent).toContain('הדגמה — לא נוצרה תמונה אמיתית');
    expect(getByTestId('media-connection').textContent).toBe('xAI לא מחובר: הדגמה בלבד');

    const demo = getByTestId('images-demo');
    expect(demo.textContent).toBe('לנסות הדגמה (בלי תמונה אמיתית)');
    expect((demo as HTMLButtonElement).disabled).toBe(true); // nothing described yet
    fireEvent.change(getByTestId('images-description'), { target: { value: 'שועל' } });
    fireEvent.click(demo);
    await waitFor(() => expect(sent(request, 'POST', '/api/media/images')).toHaveLength(1));
    expect(sent(request, 'POST', '/api/media/images')[0]).toEqual({
      description: 'שועל', style: 'none', model: 'grok-imagine-image', count: 1, aspect_ratio: '1:1', resolution: '1k', source_path: null, demo: true, lang: 'he',
    });
    expect((await findAllByTestId('job-card'))[0].getAttribute('data-kind')).toBe('image.create');
  });

  it('with xAI it shows the exact text, where it goes and the price before Create, and sends just that', async () => {
    const { findByTestId, getByTestId, request } = renderWith(<ImagesScreen />, {
      request: routes({
        'GET /api/media/status': () => mediaStatus(true),
        'GET /api/media/items': () => [],
        'POST /api/dialog': () => ({ paths: ['C:\\Users\\me\\Pictures\\fox.png'] }),
        'POST /api/media/images': () => mediaJob(),
      }),
    });
    fireEvent.change(await findByTestId('images-description'), { target: { value: '  A fox in the snow ' } });
    fireEvent.change(getByTestId('images-style'), { target: { value: 'photo' } });
    pick(getByTestId('images-count'), '2');
    expect(getByTestId('images-sends-text').textContent).toBe('A fox in the snow\n\nStyle: photorealistic photograph, natural light, high detail');
    expect(getByTestId('images-sends').textContent).toContain('Grok Imagine (xAI) (api.x.ai)');
    expect(getByTestId('images-sends-price').textContent).toBe('Estimated price: $0.04');
    expect(getByTestId('images-sends').textContent).toContain('2026-10-19');
    expect(getByTestId('images-create').textContent).toBe('Create 2 images · $0.04');

    // An image of the user's is named as sent, and billed: the High quality model, one image and the input.
    pick(getByTestId('images-model'), 'grok-imagine-image-quality');
    pick(getByTestId('images-count'), '1');
    fireEvent.click(getByTestId('images-source-open'));
    await waitFor(() => expect(getByTestId('images-sends-file').textContent).toContain('your file fox.png'));
    expect(getByTestId('images-sends-price').textContent).toBe('Estimated price: $0.06');

    fireEvent.click(getByTestId('images-create'));
    await waitFor(() => expect(sent(request, 'POST', '/api/media/images')).toHaveLength(1));
    expect(sent(request, 'POST', '/api/media/images')[0]).toEqual({
      description: '  A fox in the snow ', style: 'photo', model: 'grok-imagine-image-quality', count: 1, aspect_ratio: '1:1', resolution: '1k',
      source_path: 'C:\\Users\\me\\Pictures\\fox.png', demo: false, lang: 'en',
    });
  });

  it('marks a demonstration preview and never saves it; saves a real one as its format and replaces only when told', async () => {
    let overwrite: boolean | null = null;
    const { findAllByTestId, getByTestId, getAllByTestId, findByTestId, request } = renderWith(<ImagesScreen />, {
      request: routes({
        'GET /api/media/status': () => mediaStatus(true),
        'GET /api/media/items': () => [mediaItem(), mediaItem({ id: 'media_demo1', real: false, format: 'SVG', mime: 'image/svg+xml', usd: null, cost: null, name: 'demo-1.svg' })],
        'POST /api/dialog': () => ({ paths: ['C:\\out\\fox.jpeg'] }),
        'POST /api/media/save': (body) => {
          overwrite = (body as { overwrite: boolean }).overwrite;
          if (!overwrite) throw new ApiError(409, 'exists', 'C:\\out\\fox.png already exists. Replace it?');
          return mediaJob({ id: 'job_save', kind: 'image.save', title: 'Save the image to C:\\out\\fox.png' });
        },
      }),
    });
    const tiles = await findAllByTestId('media-item');
    expect(tiles.map((tile) => tile.getAttribute('data-real'))).toEqual(['true', 'false']);
    expect(within(tiles[1]).getByTestId('media-demo-badge').textContent).toBe('Demonstration — no real image was created');
    expect(within(tiles[0]).queryByTestId('media-demo-badge')).toBeNull();

    fireEvent.click(within(tiles[1]).getByTestId('media-item-open'));
    expect(((await findByTestId('media-save-disabled')) as HTMLButtonElement).disabled).toBe(true);
    expect(getByTestId('media-sent').textContent).toBe('Nothing was sent: this is a demonstration.');
    expect(getByTestId('images-refine-sends').textContent).toContain('nothing is sent');

    fireEvent.click(within(tiles[0]).getByTestId('media-item-open'));
    expect((await findByTestId('media-save')).textContent).toContain('so the name ends in .png');
    expect(getByTestId('media-save-pick-open').textContent).toBe('Save as PNG…');
    fireEvent.click(getByTestId('media-save-pick-open'));
    expect((await findByTestId('media-save-conflict')).textContent).toContain('already exists. Replace it?');
    expect(overwrite).toBe(false);
    fireEvent.click(getByTestId('media-save-replace'));
    await waitFor(() => expect(sent(request, 'POST', '/api/media/save')).toHaveLength(2));
    expect(sent(request, 'POST', '/api/media/save')[1]).toEqual({ item_id: 'media_real1', target: 'C:\\out\\fox.jpeg', overwrite: true, lang: 'en' });
    await waitFor(() => expect(getAllByTestId('job-card')[0].getAttribute('data-kind')).toBe('image.save'));
    expect(sent(request, 'POST', '/api/dialog')[0]).toMatchObject({ kind: 'save', save_name: 'a-fox-1.png', file_types: ['PNG (*.png)'] });
  });

  it('changes or varies a preview with the text and the price shown first, and deletes it after a second click', async () => {
    let kept = [mediaItem()];
    const { findAllByTestId, findByTestId, getByTestId, queryByTestId, request } = renderWith(<ImagesScreen />, {
      request: routes({
        'GET /api/media/status': () => mediaStatus(true),
        'GET /api/media/items': () => kept,
        'POST /api/media/images/refine': () => mediaJob({ id: 'job_refine', kind: 'image.refine' }),
        'DELETE /api/media/items/media_real1': () => {
          kept = [];
          return { deleted: true };
        },
      }),
    });
    fireEvent.click(within((await findAllByTestId('media-item'))[0]).getByTestId('media-item-open'));
    fireEvent.change(await findByTestId('images-change'), { target: { value: 'Make the sky purple' } });
    expect(getByTestId('images-refine-sends-text').textContent).toBe('Change this image as asked, keep everything else as it is: Make the sky purple\nOriginal description: A fox');
    expect(getByTestId('images-refine-sends-file').textContent).toContain('the preview a-fox-1.png');
    expect(getByTestId('images-refine').textContent).toBe('Change it · $0.02');
    fireEvent.click(getByTestId('images-refine'));
    await waitFor(() => expect(sent(request, 'POST', '/api/media/images/refine')).toHaveLength(1));
    expect(sent(request, 'POST', '/api/media/images/refine')[0]).toEqual({ item_id: 'media_real1', change: 'Make the sky purple', variation: false, count: 1, lang: 'en' });

    pick(getByTestId('images-refine-mode'), 'variation');
    expect(queryByTestId('images-change')).toBeNull();
    expect(getByTestId('images-refine-sends-text').textContent).toBe(
      'A variation of this image: keep the subject, composition and style, change the details. Original description: A fox',
    );
    fireEvent.click(getByTestId('images-refine'));
    await waitFor(() => expect(sent(request, 'POST', '/api/media/images/refine')).toHaveLength(2));
    expect(sent(request, 'POST', '/api/media/images/refine')[1]).toEqual({ item_id: 'media_real1', change: '', variation: true, count: 1, lang: 'en' });

    fireEvent.click(getByTestId('media-delete'));
    expect(sent(request, 'DELETE', '/api/media/items/media_real1')).toHaveLength(0); // armed, not yet deleted
    fireEvent.click(getByTestId('media-delete'));
    await waitFor(() => expect(queryByTestId('media-selected')).toBeNull());
    expect(sent(request, 'DELETE', '/api/media/items/media_real1')).toHaveLength(1);
    expect(await findByTestId('media-empty')).toBeTruthy();
  });
});
