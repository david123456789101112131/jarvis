import { useState } from 'react';
import { money, type DevChange, type DevFile } from '../../api/dev';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { Chip, Panel } from '../../components/ui';
import { useI18n, type MessageKey } from '../../i18n';
import { DiffView } from './DiffView';

function Lines({ title, items, testId, tone }: { title: string; items: string[]; testId: string; tone?: 'warn' }) {
  if (!items.length) return null;
  return (
    <div className={`code-lines ${tone ? `code-lines--${tone}` : ''}`} data-testid={testId}>
      <span className="field__label">{title}</span>
      <ul>
        {items.map((line, i) => (
          <li key={i}>
            <BidiText text={line} />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** One proposed file: why it changes, its diff, what JARVIS found wrong, and the user's edit or exclusion. */
function FileReview({ file, locked, busy, onInclude, onSave }: { file: DevFile; locked: boolean; busy: boolean; onInclude: (include: boolean) => void; onSave: (content: string) => Promise<boolean> }) {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(file.content);
  const id = `code-change-file-${file.path}`;
  return (
    <article className="code-file" data-testid={id} data-blocked={file.blocked || undefined} data-status={file.status}>
      <header className="code-file__head">
        {!locked ? (
          <label className="code-file__include" title={t('code.file.include')}>
            <input type="checkbox" checked={file.include} disabled={busy} onChange={(e) => onInclude(e.target.checked)} data-testid={`${id}-include`} />
          </label>
        ) : null}
        <span className="mono code-file__path" dir="ltr">{file.path}</span>
        <span className="num code-plus">+{file.added}</span>
        <span className="num code-minus">−{file.removed}</span>
        {file.new_file ? <span className="code-tag">{t('code.file.new')}</span> : null}
        {file.edited ? <span className="code-tag">{t('code.file.edited')}</span> : null}
        <span className="code-tag code-tag--muted">{file.eol}</span>
        {file.status !== 'proposed' ? <Chip tone={file.status === 'conflict' ? 'danger' : file.status === 'applied' ? 'ok' : 'muted'}>{t(`code.file.status.${file.status}` as MessageKey)}</Chip> : null}
        {!locked && file.status !== 'applied' ? (
          <button type="button" className="btn btn--sm btn--ghost" disabled={busy || !file.editable} title={file.editable ? undefined : t('code.file.notEditable')} onClick={() => { setText(file.content); setEditing((v) => !v); }} data-testid={`${id}-edit`}>
            <Icon name="name" size={13} />
            {t('code.file.edit')}
          </button>
        ) : null}
      </header>
      {file.why ? (
        <p className="code-file__why">
          <BidiText text={file.why} />
        </p>
      ) : null}
      {file.problems.length ? (
        <div className="code-notes code-notes--problem" role="alert" data-testid={`${id}-problems`}>
          <span className="field__label">
            <Icon name="alert" size={13} /> {t('code.file.problems')}
          </span>
          <ul>
            {file.problems.map((n, i) => (
              <li key={i} data-code={n.code}>
                <BidiText text={n.text} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {file.warnings.length ? (
        <div className="code-notes code-notes--warning" data-testid={`${id}-warnings`}>
          <span className="field__label">
            <Icon name="info" size={13} /> {t('code.file.warnings')}
          </span>
          <ul>
            {file.warnings.map((n, i) => (
              <li key={i} data-code={n.code}>
                <BidiText text={n.text} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {editing ? (
        <form
          className="code-file__editor"
          onSubmit={(e) => {
            e.preventDefault();
            void onSave(text).then((ok) => ok && setEditing(false));
          }}
        >
          <textarea className="textarea mono" dir="ltr" value={text} onChange={(e) => setText(e.target.value)} rows={Math.min(30, Math.max(8, text.split('\n').length))} spellCheck={false} data-testid={`${id}-text`} />
          <div className="actions-row">
            <button type="submit" className="btn btn--sm btn--primary" disabled={busy || text === file.content} data-testid={`${id}-save`}>
              {t('code.file.save')}
            </button>
            <button type="button" className="btn btn--sm btn--ghost" onClick={() => setEditing(false)}>
              {t('code.file.cancel')}
            </button>
          </div>
        </form>
      ) : file.diff ? (
        <DiffView diff={file.diff} testId={`${id}-diff`} />
      ) : null}
    </article>
  );
}

/** The proposed change to review: what was understood, the plan, each file's diff, then Apply / Discard / Undo. */
export function ChangePanel({
  change,
  engineName,
  busy,
  applying,
  onInclude,
  onSave,
  onApply,
  onUndo,
  onDiscard,
  onRevise,
}: {
  change: DevChange;
  engineName: string;
  busy: boolean;
  applying: boolean;
  onInclude: (path: string, include: boolean) => void;
  onSave: (path: string, content: string) => Promise<boolean>;
  onApply: () => void;
  onUndo: () => void;
  onDiscard: () => void;
  onRevise: (text: string) => void;
}) {
  const { t } = useI18n();
  const [revision, setRevision] = useState('');
  const name = engineName;
  const locked = change.status === 'discarded';
  const toApply = change.files.filter((f) => f.include && f.status !== 'applied');
  const applied = change.files.some((f) => f.status === 'applied');
  const blocked = toApply.some((f) => f.blocked);
  const why = !toApply.length ? t('code.applyNothing') : blocked ? t('code.applyBlocked') : !change.writable ? t('code.applyNeedsAllow') : '';
  return (
    <Panel
      title={t('code.change')}
      icon="code"
      testId="code-change"
      meta={<Chip tone={change.status === 'applied' ? 'ok' : change.status === 'discarded' ? 'muted' : 'accent'} testId="code-change-status">{t(`code.status.${change.status}` as MessageKey)}</Chip>}
    >
      <div className="area__stack">
        <p className="field__hint">
          {t('code.changeBy', { model: change.model || name })}
          {change.cost_usd !== null ? ` · ${t('code.cost', { cost: money(change.cost_usd) })}` : ''}
          {change.masked ? ` · ${t('code.maskedNote', { n: change.masked })}` : ''}
        </p>
        {change.understood ? (
          <div className="code-understood" data-testid="code-understood">
            <span className="field__label">{t('code.understood', { name })}</span>
            <BidiText text={change.understood} />
          </div>
        ) : null}
        <Lines title={t('code.planSteps')} items={change.plan} testId="code-plan-steps" />
        <Lines title={t('code.assumptions')} items={change.assumptions} testId="code-assumptions" tone="warn" />
        <Lines title={t('code.questions', { name })} items={change.questions} testId="code-questions" tone="warn" />
        <Lines title={t('code.risks')} items={change.risks} testId="code-risks" />
        {change.summary ? (
          <div data-testid="code-summary">
            <span className="field__label">{t('code.summary')}</span>
            <BidiText text={change.summary} />
          </div>
        ) : null}
        {change.tests.notes || change.tests.command ? (
          <p className="field__hint" data-testid="code-tests-advice">
            <strong>{t('code.testsAdvice')}: </strong>
            <BidiText text={[change.tests.notes, change.tests.command].filter(Boolean).join(' — ')} />
          </p>
        ) : null}

        <div className="code-change-files">
          {change.files.map((f) => (
            <FileReview key={`${change.id}-${f.path}-${f.edited}`} file={f} locked={locked} busy={busy} onInclude={(include) => onInclude(f.path, include)} onSave={(content) => onSave(f.path, content)} />
          ))}
        </div>

        {!locked ? (
          <div className="actions-row code-change-actions">
            {toApply.length ? (
              <button type="button" className="btn btn--primary" disabled={busy || applying || Boolean(why)} title={why || undefined} onClick={onApply} data-testid="code-apply">
                <Icon name="check" size={14} />
                {applying ? t('code.applying') : t('code.apply')}
              </button>
            ) : null}
            {applied ? (
              <button type="button" className="btn" disabled={busy || applying || !change.writable} title={change.writable ? undefined : t('code.applyNeedsAllow')} onClick={onUndo} data-testid="code-undo">
                <Icon name="undo" size={14} />
                {t('code.undo')}
              </button>
            ) : (
              <button type="button" className="btn btn--ghost" disabled={busy || applying} onClick={onDiscard} data-testid="code-discard">
                <Icon name="trash" size={14} />
                {t('code.discard')}
              </button>
            )}
            {why && toApply.length ? <span className="field__hint" data-testid="code-apply-why">{why}</span> : null}
          </div>
        ) : null}

        {!locked && !applied ? (
          <form
            className="code-revise"
            onSubmit={(e) => {
              e.preventDefault();
              if (revision.trim().length < 2) return;
              onRevise(revision.trim());
              setRevision('');
            }}
          >
            <label className="field">
              <span className="field__label">{t('code.revise')}</span>
              <textarea className="textarea" rows={2} value={revision} onChange={(e) => setRevision(e.target.value)} placeholder={t('code.revisePlaceholder')} disabled={busy} data-testid="code-revise-text" />
            </label>
            <button type="submit" className="btn btn--sm" disabled={busy || revision.trim().length < 2} data-testid="code-revise">
              <Icon name="sparkles" size={13} />
              {t('code.reviseSend', { name })}
            </button>
          </form>
        ) : null}
      </div>
    </Panel>
  );
}
