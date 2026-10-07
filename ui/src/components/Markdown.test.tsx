import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown, parseBlocks, parseInline, splitRow } from './Markdown';

describe('parseBlocks', () => {
  it('reads paragraphs, headings, lists and code fences', () => {
    const blocks = parseBlocks('# כותרת\n\nפסקה ראשונה\nשורה שנייה\n\n- א\n- ב\n\n1. one\n2. two\n\n```\ncode <b>\n```');
    expect(blocks.map((b) => b.type)).toEqual(['h', 'p', 'ul', 'ol', 'code']);
  });

  it('reads a table with column alignment', () => {
    const [table] = parseBlocks('| שם | כמות |\n|:---|---:|\n| א | 1 |\n| ב | 2 |');
    expect(table).toEqual({
      type: 'table',
      header: ['שם', 'כמות'],
      align: ['left', 'right'],
      rows: [
        ['א', '1'],
        ['ב', '2'],
      ],
    });
  });

  it('does not take a lone pipe line for a table', () => {
    expect(parseBlocks('a | b\nno separator').map((b) => b.type)).toEqual(['p']);
    expect(splitRow('| a \\| b | c |')).toEqual(['a | b', 'c']);
  });
});

describe('parseInline', () => {
  it('reads bold, italic, code and safe links', () => {
    const nodes = parseInline('**חשוב** *נטוי* `x` [אתר](https://example.com)');
    expect(nodes.filter((n) => n.type !== 'text').map((n) => n.type)).toEqual(['strong', 'em', 'code', 'link']);
  });

  it('leaves unsafe link schemes as plain text', () => {
    const nodes = parseInline('[click](javascript:alert(1))');
    expect(nodes.every((n) => n.type === 'text')).toBe(true);
  });
});

describe('Markdown', () => {
  it('never turns message text into markup', () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)"> <script>alert(2)</script>'} />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain('<script>alert(2)</script>');
  });

  it('renders tables and isolates paths in text', () => {
    const { container } = render(<Markdown text={'נשמר ב-C:\\work\\a.txt\n\n| a | b |\n|---|---|\n| 1 | 2 |'} />);
    expect(container.querySelector('table')).not.toBeNull();
    expect(container.querySelectorAll('tbody tr')).toHaveLength(1);
    expect(container.querySelector('bdi')?.textContent).toBe('C:\\work\\a.txt');
  });

  it('opens links outside the app window without giving the page an opener', () => {
    const { container } = render(<Markdown text="[docs](https://example.com/docs)" />);
    const a = container.querySelector('a');
    expect(a?.getAttribute('rel')).toContain('noopener');
    expect(a?.getAttribute('href')).toBe('https://example.com/docs');
  });
});
