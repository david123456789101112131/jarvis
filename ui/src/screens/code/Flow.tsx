import type { DevChange, TestsResult } from '../../api/dev';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { Panel } from '../../components/ui';
import { useI18n, type MessageKey } from '../../i18n';

export type FlowStep = 'request' | 'project' | 'plan' | 'change' | 'tests' | 'summary';
export type FlowState = 'todo' | 'active' | 'done' | 'failed';

const STEPS: FlowStep[] = ['request', 'project', 'plan', 'change', 'tests', 'summary'];
const ICON: Record<FlowState, string> = { todo: 'clock', active: 'bolt', done: 'check', failed: 'x' };

/** understand the request → look at the project → a short plan → the change → tests → summary. */
export function Flow({ states }: { states: Record<FlowStep, FlowState> }) {
  const { t } = useI18n();
  return (
    <ol className="code-flow" aria-label={t('code.flow')} data-testid="code-flow">
      {STEPS.map((step, i) => (
        <li key={step} className="code-flow__step" data-state={states[step]} data-testid={`code-flow-${step}`}>
          <span className="code-flow__num num">{i + 1}</span>
          <Icon name={ICON[states[step]]} size={12} />
          <span>{t(`code.flow.${step}` as MessageKey)}</span>
        </li>
      ))}
    </ol>
  );
}

/** What was done: the files changed, what the tests said, and the next step. */
export function DonePanel({ change, tests }: { change: DevChange; tests: TestsResult | null }) {
  const { t } = useI18n();
  const applied = change.files.filter((f) => f.status === 'applied');
  const next = !tests ? 'code.done.nextTests' : tests.ok ? 'code.done.nextOk' : 'code.done.nextFix';
  return (
    <Panel title={t('code.done')} icon="check" testId="code-done">
      <dl className="code-done">
        <dt>{t('code.done.changed')}</dt>
        <dd>
          <ul className="code-done__files">
            {applied.map((f) => (
              <li key={f.path}>
                <span className="mono" dir="ltr">{f.path}</span> <span className="num code-plus">+{f.added}</span> <span className="num code-minus">−{f.removed}</span>
                {f.new_file ? <span className="code-tag">{t('code.file.new')}</span> : null}
              </li>
            ))}
          </ul>
          {change.summary ? <BidiText text={change.summary} /> : null}
        </dd>
        <dt>{t('code.done.tests')}</dt>
        <dd data-testid="code-done-tests">{tests ? <BidiText text={tests.summary} /> : t('code.done.notRun')}</dd>
        <dt>{t('code.done.next')}</dt>
        <dd>{t(next)}</dd>
      </dl>
    </Panel>
  );
}
