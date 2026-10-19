import type { DevReport, DevSession, InspectResult } from '../../api/dev';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { Chip, Panel, Toggle } from '../../components/ui';
import { PathPicker } from '../../components/work/PathPicker';
import { useI18n } from '../../i18n';

/** The project: the folder the user picked, what JARVIS found in it, and the "allow changes" switch. */
export function ProjectPanel({
  session,
  report,
  git,
  opening,
  onPick,
  onAllow,
  onClose,
  onRefresh,
  busy,
}: {
  session: DevSession | null;
  report: DevReport | null;
  git: InspectResult['git'];
  opening: boolean;
  onPick: (path: string) => void;
  onAllow: (allow: boolean) => void;
  onClose: () => void;
  onRefresh: () => void;
  busy: boolean;
}) {
  const { t, lang } = useI18n();
  return (
    <Panel
      title={t('code.project')}
      icon="folder"
      testId="code-project"
      actions={
        session ? (
          <>
            <button type="button" className="btn btn--sm btn--ghost" disabled={busy} onClick={onRefresh} data-testid="code-refresh">
              <Icon name="replay" size={13} />
              {t('code.refresh')}
            </button>
            <button type="button" className="btn btn--sm btn--ghost" disabled={busy} onClick={onClose} data-testid="code-close">
              <Icon name="x" size={13} />
              {t('code.close')}
            </button>
          </>
        ) : null
      }
    >
      <div className="area__stack">
        <PathPicker kind="folder" value={session?.root ?? null} onChange={(path) => path && onPick(path)} label={t('code.pickProject')} testId="code-pick" disabled={opening || busy} />
        {!session ? <p className="field__hint">{t('code.pickHint')}</p> : null}
        {opening ? <p className="field__hint" data-testid="code-opening">{t('code.opening')}</p> : null}

        {report ? (
          <dl className="code-report" data-testid="code-report">
            <dt>{t('code.languages')}</dt>
            <dd>
              {report.languages.slice(0, 4).map((l) => (
                <Chip key={l.name} tone="steel">
                  {l.name} <span className="num">{l.files}</span>
                </Chip>
              ))}
              <span className="field__hint">{t('code.files', { n: report.files })}</span>
            </dd>
            {report.frameworks.length ? (
              <>
                <dt>{t('code.frameworks')}</dt>
                <dd className="code-report__wrap">{report.frameworks.join(' · ')}</dd>
              </>
            ) : null}
            <dt>{t('code.testCommand')}</dt>
            <dd className="mono" dir="ltr" data-testid="code-test-command">{report.commands.test || t('code.noTests')}</dd>
            {report.commands.run.length ? (
              <>
                <dt>{t('code.runCommands')}</dt>
                <dd className="mono" dir="ltr">{report.commands.run.slice(0, 3).join(' · ')}</dd>
              </>
            ) : null}
            {report.style.indent || report.style.line_endings ? (
              <>
                <dt>{t('code.style')}</dt>
                <dd>{[report.style.indent, report.style.line_endings, report.style.js_quotes ? `${report.style.js_quotes} quotes` : ''].filter(Boolean).join(' · ')}</dd>
              </>
            ) : null}
            {git && git.ok ? (
              <>
                <dt>{t('code.git')}</dt>
                <dd>{git.changed ? t('code.gitChanged', { branch: git.branch ?? '', n: git.changed }) : t('code.gitClean', { branch: git.branch ?? '' })}</dd>
              </>
            ) : null}
            {report.secret_files.length ? (
              <>
                <dt>{t('code.secretFiles')}</dt>
                <dd className="mono code-report__wrap" dir="ltr" data-testid="code-secret-files">
                  {report.secret_files.slice(0, 6).join(', ')}
                  {report.secret_files.length > 6 ? ` +${report.secret_files.length - 6}` : ''}
                </dd>
              </>
            ) : null}
            {report.missing.length ? (
              <>
                <dt>{t('code.missing')}</dt>
                <dd>
                  <ul className="code-report__missing" data-testid="code-missing">
                    {report.missing.map((m, i) => (
                      <li key={m.key ?? i}>
                        <BidiText text={lang === 'he' ? m.he : m.en} />
                      </li>
                    ))}
                  </ul>
                </dd>
              </>
            ) : null}
          </dl>
        ) : null}

        {session ? (
          <div className="code-allow" data-testid="code-allow">
            <Toggle checked={session.writable} onChange={onAllow} label={t('code.allowChanges')} disabled={busy} testId="code-allow-toggle" />
            <p className="field__hint">{session.writable ? t('code.allowedNow') : t('code.allowHint')}</p>
          </div>
        ) : null}
      </div>
    </Panel>
  );
}
