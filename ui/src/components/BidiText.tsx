import { Fragment, useMemo } from 'react';

/**
 * File paths and web addresses inside Hebrew sentences are left-to-right islands. Without an
 * isolate the bidi algorithm reorders their segments ("C:\Users\…" shows with the drive letter at
 * the far end), so they are wrapped in `<bdi dir="ltr">`.
 */

const URL = String.raw`https?:\/\/[^\s<>"'\`]+`;
// A Windows path; it may continue past a space while the next word still holds a separator
// ("C:\Users\me\My Documents\a.txt"), which is how folder names with spaces usually appear.
const WINDOWS = String.raw`(?:\b[A-Za-z]:[\\/]|\\\\)[^\s<>"|?*\`]*(?: [^\s<>"|?*\`\\/]+[\\/][^\s<>"|?*\`]*)*`;
// An absolute POSIX path (or ~/…) of at least two segments, starting a word or after a Hebrew "ב-".
const POSIX = String.raw`(?<=^|[\s(\["'-])~?\/(?:[^\s\/<>"'\`]+\/)+[^\s<>"'\`]*`;
const TOKEN = new RegExp(`${URL}|${WINDOWS}|${POSIX}`, 'g');
const TRAILING = new Set(['.', ',', ';', ':', '!', '?', "'", '"', ')', ']', '}', '»', '״', '׳']);

export type BidiPart = { text: string; ltr: boolean };

/** Sentence punctuation right after a path belongs to the sentence ("…\a.txt."), but a closing
 *  parenthesis stays when the path opened one ("C:\Program Files (x86)"). */
function trimTrailing(token: string): string {
  let end = token.length;
  while (end > 0 && TRAILING.has(token[end - 1])) {
    const ch = token[end - 1];
    if (ch === ')') {
      const body = token.slice(0, end);
      const open = body.split('(').length - 1;
      const close = body.split(')').length - 1;
      if (open >= close) break;
    }
    end -= 1;
  }
  return token.slice(0, end);
}

/** Split text into plain runs and left-to-right runs (paths and URLs). */
export function splitBidi(text: string): BidiPart[] {
  const parts: BidiPart[] = [];
  const re = new RegExp(TOKEN.source, 'g');
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text))) {
    const token = trimTrailing(match[0]);
    if (!token) {
      re.lastIndex = match.index + 1;
      continue;
    }
    if (match.index > last) parts.push({ text: text.slice(last, match.index), ltr: false });
    parts.push({ text: token, ltr: true });
    last = match.index + token.length;
    re.lastIndex = last;
  }
  if (last < text.length) parts.push({ text: text.slice(last), ltr: false });
  return parts;
}

export function BidiText({ text }: { text: string }) {
  const parts = useMemo(() => splitBidi(text), [text]);
  return (
    <>
      {parts.map((part, i) =>
        part.ltr ? (
          <bdi key={i} dir="ltr" className="bidi-run">
            {part.text}
          </bdi>
        ) : (
          <Fragment key={i}>{part.text}</Fragment>
        ),
      )}
    </>
  );
}
