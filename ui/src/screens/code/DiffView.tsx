import { useMemo } from 'react';
import { diffLines } from '../../api/dev';

/** A unified diff, coloured line by line (always left to right: it is code). */
export function DiffView({ diff, testId }: { diff: string; testId?: string }) {
  const lines = useMemo(() => diffLines(diff), [diff]);
  return (
    <pre className="diff" dir="ltr" data-testid={testId ?? 'diff'}>
      {lines.map((line, i) => (
        <span key={i} className={`diff__line diff__line--${line.kind}`}>
          {line.text || ' '}
          {'\n'}
        </span>
      ))}
    </pre>
  );
}
