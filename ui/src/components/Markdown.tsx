import type { ReactNode } from 'react';
import { BidiText } from './BidiText';

// "Markdown-light" for chat messages and task results: paragraphs, line breaks, **bold**, *italic*,
// `code`, ``` fenced blocks ```, bullet / numbered lists, # headings, | tables | and [links](https://…).
// Builds React elements directly — no HTML strings, so message text can never inject markup.

export type Align = 'left' | 'center' | 'right' | null;

export type Block =
  | { type: 'p'; text: string }
  | { type: 'h'; text: string }
  | { type: 'ul'; items: string[] }
  | { type: 'ol'; items: string[]; start: number }
  | { type: 'code'; text: string; lang: string }
  | { type: 'table'; header: string[]; align: Align[]; rows: string[][] };

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*(\d+)[.)]\s+(.*)$/;
const HEADING = /^\s{0,3}#{1,4}\s+(.*)$/;
const FENCE = /^\s*```\s*([\w-]*)\s*$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

/** Cells of one `| a | b |` row (outer pipes optional, `\|` is a literal pipe). */
export function splitRow(line: string): string[] {
  let body = line.trim();
  if (body.startsWith('|')) body = body.slice(1);
  if (body.endsWith('|') && !body.endsWith('\\|')) body = body.slice(0, -1);
  return body.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'));
}

/** A header row followed by a delimiter row with the same number of cells (as in GFM). */
function isTableStart(lines: string[], i: number): boolean {
  if (!lines[i].includes('|') || i + 1 >= lines.length || !TABLE_SEPARATOR.test(lines[i + 1])) return false;
  return splitRow(lines[i]).length === splitRow(lines[i + 1]).length;
}

function cellAlign(spec: string): Align {
  const left = spec.startsWith(':');
  const right = spec.endsWith(':');
  if (left && right) return 'center';
  if (right) return 'right';
  if (left) return 'left';
  return null;
}

/** Split text into blocks. */
export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let para: string[] = [];
  const flush = () => {
    if (para.length) {
      blocks.push({ type: 'p', text: para.join('\n') });
      para = [];
    }
  };
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const fence = FENCE.exec(line);
    if (fence) {
      flush();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !FENCE.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      blocks.push({ type: 'code', text: body.join('\n'), lang: fence[1] ?? '' });
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    if (isTableStart(lines, i)) {
      flush();
      const header = splitRow(line);
      const align = splitRow(lines[i + 1]).map(cellAlign);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      i -= 1;
      blocks.push({ type: 'table', header, align, rows });
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      blocks.push({ type: 'h', text: heading[1] });
      continue;
    }
    const bullet = BULLET.exec(line);
    if (bullet) {
      flush();
      const last = blocks[blocks.length - 1];
      if (last?.type === 'ul') last.items.push(bullet[1]);
      else blocks.push({ type: 'ul', items: [bullet[1]] });
      continue;
    }
    const numbered = NUMBERED.exec(line);
    if (numbered) {
      flush();
      const last = blocks[blocks.length - 1];
      if (last?.type === 'ol') last.items.push(numbered[2]);
      else blocks.push({ type: 'ol', items: [numbered[2]], start: Number(numbered[1]) || 1 });
      continue;
    }
    para.push(line);
  }
  flush();
  return blocks;
}

export type Inline =
  | { type: 'text'; text: string }
  | { type: 'strong'; children: Inline[] }
  | { type: 'em'; children: Inline[] }
  | { type: 'code'; text: string }
  | { type: 'link'; text: string; href: string }
  | { type: 'br' };

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*)|(\[[^\]\n]+\]\((?:https?:\/\/|mailto:)[^)\s]+\))|((?:^|(?<=[\s(]))\*[^*\s][^*\n]*?\*(?=[\s.,;:!?)]|$))|(\n)/g;

/** Parse inline formatting. Unsafe link schemes are not recognised (left as text). */
export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  // A fresh regex per call: parsing recurses (bold/italic children) and `g` regexes are stateful.
  const re = new RegExp(INLINE.source, 'g');
  let match: RegExpExecArray | null;
  while ((match = re.exec(text))) {
    if (match.index > last) out.push({ type: 'text', text: text.slice(last, match.index) });
    const [token] = match;
    if (match[1]) out.push({ type: 'code', text: token.slice(1, -1) });
    else if (match[2]) out.push({ type: 'strong', children: parseInline(token.slice(2, -2)) });
    else if (match[3]) {
      const close = token.indexOf('](');
      out.push({ type: 'link', text: token.slice(1, close), href: token.slice(close + 2, -1) });
    } else if (match[4]) out.push({ type: 'em', children: parseInline(token.slice(1, -1)) });
    else if (match[5]) out.push({ type: 'br' });
    last = match.index + token.length;
  }
  if (last < text.length) out.push({ type: 'text', text: text.slice(last) });
  return out;
}

function renderInline(nodes: Inline[]): ReactNode[] {
  return nodes.map((node, i) => {
    switch (node.type) {
      case 'text':
        return <BidiText key={i} text={node.text} />;
      case 'strong':
        return <strong key={i}>{renderInline(node.children)}</strong>;
      case 'em':
        return <em key={i}>{renderInline(node.children)}</em>;
      case 'code':
        return (
          <code key={i} className="md__code">
            {node.text}
          </code>
        );
      case 'link':
        return (
          <a key={i} href={node.href} target="_blank" rel="noopener noreferrer">
            {node.text}
          </a>
        );
      case 'br':
        return <br key={i} />;
      default:
        return null;
    }
  });
}

export function Markdown({ text, className, dir }: { text: string; className?: string; dir?: 'rtl' | 'ltr' }) {
  const blocks = parseBlocks(text);
  return (
    <div className={`md ${className ?? ''}`} dir={dir}>
      {blocks.map((block, i) => {
        switch (block.type) {
          case 'p':
            return <p key={i}>{renderInline(parseInline(block.text))}</p>;
          case 'h':
            return (
              <p key={i} className="md__h">
                {renderInline(parseInline(block.text))}
              </p>
            );
          case 'ul':
            return (
              <ul key={i}>
                {block.items.map((item, j) => (
                  <li key={j}>{renderInline(parseInline(item))}</li>
                ))}
              </ul>
            );
          case 'ol':
            return (
              <ol key={i} start={block.start}>
                {block.items.map((item, j) => (
                  <li key={j}>{renderInline(parseInline(item))}</li>
                ))}
              </ol>
            );
          case 'code':
            return (
              <pre key={i} className="md__pre" dir="ltr">
                <code>{block.text}</code>
              </pre>
            );
          case 'table':
            return (
              <div key={i} className="md__table-wrap">
                <table className="md__table">
                  <thead>
                    <tr>
                      {block.header.map((cell, j) => (
                        <th key={j} style={block.align[j] ? { textAlign: block.align[j]! } : undefined}>
                          {renderInline(parseInline(cell))}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, r) => (
                      <tr key={r}>
                        {block.header.map((_, j) => (
                          <td key={j} style={block.align[j] ? { textAlign: block.align[j]! } : undefined}>
                            {renderInline(parseInline(row[j] ?? ''))}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          default:
            return null;
        }
      })}
    </div>
  );
}
