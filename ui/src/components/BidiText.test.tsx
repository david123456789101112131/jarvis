import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { BidiText, splitBidi } from './BidiText';

const ltr = (text: string) => splitBidi(text).filter((p) => p.ltr).map((p) => p.text);

describe('splitBidi', () => {
  it('isolates a Windows path inside a Hebrew sentence', () => {
    expect(ltr('נוצרה התיקייה: C:\\Users\\USER\\Documents\\דוחות')).toEqual(['C:\\Users\\USER\\Documents\\דוחות']);
  });

  it('keeps sentence punctuation outside the path', () => {
    const parts = splitBidi('הקובץ נשמר ב-C:\\work\\a.txt.');
    expect(parts.map((p) => p.text).join('')).toBe('הקובץ נשמר ב-C:\\work\\a.txt.');
    expect(ltr('הקובץ נשמר ב-C:\\work\\a.txt.')).toEqual(['C:\\work\\a.txt']);
  });

  it('follows a path across a space while the next word is still part of it', () => {
    expect(ltr('נפתח C:\\Users\\me\\My Documents\\a.txt עכשיו')).toEqual(['C:\\Users\\me\\My Documents\\a.txt']);
  });

  it('isolates web addresses and POSIX paths but not words with a slash', () => {
    expect(ltr('האתר רץ ב-http://127.0.0.1:5173/ ואפשר לבדוק')).toEqual(['http://127.0.0.1:5173/']);
    expect(ltr('נכתב אל /tmp/jarvis/workspace/plan.md')).toEqual(['/tmp/jarvis/workspace/plan.md']);
    expect(ltr('נשמרה ב-/tmp/claude-0/-home-claude/plan.md.')).toEqual(['/tmp/claude-0/-home-claude/plan.md']);
    expect(ltr('לקרוא ו/או לכתוב')).toEqual([]);
    expect(ltr('just text')).toEqual([]);
  });

  it('keeps a closing parenthesis that belongs to the path', () => {
    expect(ltr('(ראו C:\\data\\run(1)\\log.txt)')).toEqual(['C:\\data\\run(1)\\log.txt']);
  });

  it('loses no text', () => {
    const text = 'א C:\\x\\y ב https://example.com/a?b=1, ג /var/log/x.log.';
    expect(splitBidi(text).map((p) => p.text).join('')).toBe(text);
  });
});

describe('BidiText', () => {
  it('renders paths as left-to-right isolates', () => {
    const { container } = render(<BidiText text="נשמר ב-C:\work\a.txt" />);
    const bdi = container.querySelector('bdi');
    expect(bdi?.getAttribute('dir')).toBe('ltr');
    expect(bdi?.textContent).toBe('C:\\work\\a.txt');
    expect(container.textContent).toBe('נשמר ב-C:\\work\\a.txt');
  });
});
