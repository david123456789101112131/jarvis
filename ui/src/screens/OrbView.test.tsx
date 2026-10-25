import { cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createInitialState } from '../state/reducer';
import { createStore } from '../state/store';
import { renderWith } from '../test/render';
import { CLICK_SLOP, OrbView } from './OrbView';

describe('OrbView (the mini orb window)', () => {
  beforeEach(() => {
    // jsdom lays nothing out: give the stage the orb window's size so the orb is drawn
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, top: 0, left: 0, right: 260, bottom: 260, width: 260, height: 260, toJSON: () => ({}),
    } as DOMRect);
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  function show() {
    const windowAction = vi.fn(async (_action: string) => ({ ok: true }));
    const store = createStore({ ...createInitialState(), hydrated: true });
    renderWith(<OrbView />, { store, lang: 'he', api: { windowAction } });
    return { windowAction, orb: screen.getByTestId('orb-button') };
  }

  it('is only the orb: no ring of quick tools, no buttons under it', () => {
    const { orb } = show();
    expect(screen.queryByRole('toolbar')).toBeNull();
    expect(screen.queryByTestId('quick-tools')).toBeNull();
    expect(screen.getAllByRole('button')).toEqual([orb]);
    fireEvent.click(orb);
    expect(screen.queryByTestId('quick-tools')).toBeNull();
  });

  it('opens JARVIS on a click, and not after the orb was dragged', () => {
    const { windowAction, orb } = show();
    const view = screen.getByTestId('orb-view');
    fireEvent.mouseDown(view, { screenX: 100, screenY: 100 });
    fireEvent.click(orb, { screenX: 100 + CLICK_SLOP + 40, screenY: 100 });      // moved: a drag
    expect(windowAction).not.toHaveBeenCalled();
    fireEvent.mouseDown(view, { screenX: 300, screenY: 300 });
    fireEvent.click(orb, { screenX: 302, screenY: 301 });
    expect(windowAction).toHaveBeenCalledWith('show');
    expect(orb.getAttribute('aria-label')).toBe('פתיחת מרכז הפיקוד');
  });
});
