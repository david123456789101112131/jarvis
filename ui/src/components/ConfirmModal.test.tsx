import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { I18nProvider } from '../i18n';
import { DetailValue } from './ConfirmModal';

const show = (value: unknown) => render(<I18nProvider initial="he"><DetailValue value={value} /></I18nProvider>).container;

describe('approval details', () => {
  it('lists every path left to right', () => {
    const box = show(['C:\\Users\\USER\\Desktop\\ישן', 'C:\\data\\a.txt']);
    expect([...box.querySelectorAll('li bdi')].map((b) => [b.getAttribute('dir'), b.textContent])).toEqual([
      ['ltr', 'C:\\Users\\USER\\Desktop\\ישן'],
      ['ltr', 'C:\\data\\a.txt'],
    ]);
  });

  it('says yes or no in words and shows the whole input', () => {
    expect(show(true).textContent).toBe('כן');
    const long = { command: 'x'.repeat(500) };
    expect(show(long).querySelector('pre')?.textContent).toContain('x'.repeat(500));
  });
});
