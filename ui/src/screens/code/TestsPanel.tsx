import type { TestsResult } from '../../api/dev';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { Chip, Panel } from '../../components/ui';
import { useI18n } from '../../i18n';
import { formatMs } from '../../lib/format';

/** Run the project's own tests, and show what ran, what passed and what failed (and offer a fix). */
export function TestsPanel({
  command,
  enabled,
  running,
  result,
  engineName,
  canFix,
  onRun,
  onFix,
}: {
  command: string;
  enabled: boolean;
  running: boolean;
  result: TestsResult | null;
  engineName: string;
  canFix: boolean;
  onRun: () => void;
  onFix: () => void;
}) {
  const { t, lang } = useI18n();
  const counts = result ? Object.entries(result.counts) : [];
  return (
    <Panel title={t('code.tests')} icon="check" testId="code-tests">
      <div className="area__stack">
        <p className="field__hint">{command ? t('code.runTestsHint', { command }) : t('code.runTestsNone')}</p>
        <div className="actions-row">
          <button type="button" className="btn" disabled={!enabled || running || !command} onClick={onRun} data-testid="code-run-tests">
            <Icon name="play" size={14} />
            {t('code.runTests')}
          </button>
          {result && !result.ok && canFix ? (
            <button type="button" className="btn btn--primary" disabled={!enabled || running} onClick={onFix} title={t('code.fixHint')} data-testid="code-fix">
              <Icon name="wand" size={14} />
              {t('code.fix', { name: engineName })}
            </button>
          ) : null}
        </div>
        {result ? (
          <div className="code-results" data-testid="code-test-results" data-ok={result.ok || undefined}>
            <div className="code-results__head">
              <Chip tone={result.ok ? 'ok' : 'danger'} testId="code-tests-verdict">
                <Icon name={result.ok ? 'check' : 'x'} size={12} />
                {result.ok ? t('code.testsPassed') : t('code.testsFailed')}
              </Chip>
              {counts.map(([k, v]) => (
                <span key={k} className={`code-count code-count--${k}`} data-testid={`code-count-${k}`}>
                  <span className="num">{v}</span> {k}
                </span>
              ))}
              {result.duration_s !== null ? <span className="num field__hint">{formatMs(result.duration_s * 1000, lang)}</span> : null}
            </div>
            <p>
              <strong>{t('code.testsRan')}: </strong>
              <span className="mono" dir="ltr" data-testid="code-tests-command">{result.command || result.runner}</span>
              {result.exit_code !== null && result.exit_code !== 0 ? <span className="field__hint"> · {t('code.exit', { code: result.exit_code })}</span> : null}
              {result.timed_out ? <span className="field__hint"> · {t('code.timedOut')}</span> : null}
            </p>
            {result.hint ? (
              <p className="code-hint" role="note">
                <Icon name="info" size={13} /> <BidiText text={result.hint} />
              </p>
            ) : null}
            {result.failures.length ? (
              <div className="code-lines code-lines--warn" data-testid="code-failures">
                <span className="field__label">{t('code.failures')}</span>
                <ul>
                  {result.failures.map((f) => (
                    <li key={f.name}>
                      <span className="mono" dir="ltr">{f.name}</span>
                      {f.message ? <span className="field__hint"> — <BidiText text={f.message} /></span> : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {result.output ? (
              <details className="code-output">
                <summary>
                  {t('code.output')}
                  {result.output_cut ? ` (${t('code.outputCut')})` : ''}
                </summary>
                <pre className="mono-block" dir="ltr" data-testid="code-output">{result.output}</pre>
              </details>
            ) : null}
          </div>
        ) : null}
      </div>
    </Panel>
  );
}
