import { proposalOf, type StepProposal } from '../../api/apps';
import type { RiskLevel } from '../../api/types';
import type { Job } from '../../api/work';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { riskTone } from '../../components/tones';
import { Chip, ConfirmButton, Panel, ProgressBar, StatusChip } from '../../components/ui';
import { JOB_TONE, Unavailable } from '../../components/work/JobCard';
import { useI18n, type MessageKey } from '../../i18n';
import { GatedButton, type Gates } from './parts';

const LAUNCH_STEPS = new Set(['open_app', 'goto']);

/** "What should be done?": the goal, the one suggested next step, and the steps done so far in this task. */
export function TaskPanel({ gates, goal, onGoal, onSuggest, pending, suggestion, step, dismissed, onDismiss, onDo, doing, done, taskId, onEnd, ending }: { gates: Gates; goal: string; onGoal: (goal: string) => void; onSuggest: () => void; pending: boolean; suggestion: Job | undefined; step: Job | undefined; dismissed: boolean; onDismiss: () => void; onDo: (suggestionId: string) => void; doing: boolean; done: string[]; taskId: string | null; onEnd: () => void; ending: boolean }) {
  const { t } = useI18n();
  const vision = gates.ways.vision;
  const why = gates.model ?? gates.busy ?? (goal.trim().length < 2 ? t('apps.task.needGoal') : null);
  const proposal = suggestion?.status === 'done' ? proposalOf(suggestion) : null;
  return (
    <Panel
      title={t('apps.task.title')}
      icon="wand"
      testId="apps-task"
      bodyClassName="apps-panel"
      actions={
        taskId ? (
          <ConfirmButton className="btn btn--sm btn--ghost" confirmLabel={t('apps.task.endConfirm')} disabled={ending} title={t('apps.task.end')} onConfirm={onEnd} testId="apps-task-end">
            {t('apps.task.end')}
          </ConfirmButton>
        ) : null
      }
    >
      {gates.computerOn && !gates.screen && gates.model && vision ? <Unavailable title={t('apps.model.title')} reason={gates.model} needs={vision.needs} testId="apps-model-unavailable" /> : null}
      <textarea
        className="textarea apps-goal"
        value={goal}
        maxLength={1000}
        placeholder={t('apps.task.placeholder')}
        aria-label={t('apps.task.title')}
        onChange={(e) => onGoal(e.target.value)}
        data-testid="apps-goal"
      />
      <GatedButton why={why} pending={pending} onClick={onSuggest} icon="wand" testId="apps-suggest">
        {t('apps.task.suggest')}
      </GatedButton>
      <p className="hint">{t('apps.task.hint')}</p>

      {suggestion?.status === 'running' ? (
        <div className="apps-live" data-testid="apps-suggest-live">
          <ProgressBar value={suggestion.progress ?? 0} indeterminate={suggestion.progress === null} tone="work" label={suggestion.message || t('apps.task.running')} />
          <span className="hint">
            <BidiText text={suggestion.message || t('apps.task.running')} />
          </span>
        </div>
      ) : null}
      {suggestion && (suggestion.status === 'failed' || suggestion.status === 'cancelled') ? (
        <p className="apps-why" data-testid="apps-suggest-failed">
          <Icon name="alert" size={12} />
          <BidiText text={t('apps.task.failed', { reason: suggestion.error || suggestion.message || suggestion.status })} />
        </p>
      ) : null}
      {suggestion && proposal && !dismissed ? <SuggestionCard id={suggestion.id} proposal={proposal} step={step} gates={gates} onDo={onDo} doing={doing} onDismiss={onDismiss} /> : null}

      {done.length ? (
        <div className="apps-done">
          <span className="job__label">{t('apps.task.done')}</span>
          <ol data-testid="apps-done">
            {done.map((line, i) => (
              <li key={i}>
                <BidiText text={line} />
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </Panel>
  );
}

/** The one suggested step: in words, where it lands, how risky JARVIS's rules judge it, what it would do (for
 *  sending, deleting, paying…), and Do (which asks for approval first) or Dismiss. */
export function SuggestionCard({ id, proposal, step, gates, onDo, doing, onDismiss }: { id: string; proposal: StepProposal; step: Job | undefined; gates: Gates; onDo: (id: string) => void; doing: boolean; onDismiss: () => void }) {
  const { t } = useI18n();
  const action = proposal.kind === 'action';
  const risk = (proposal.risk ?? 'low') as RiskLevel;
  const kind = String(proposal.action?.action ?? '');
  const doWhy = LAUNCH_STEPS.has(kind) ? gates.launch ?? gates.busy : gates.screen ?? gates.busy;
  return (
    <article className="apps-suggestion" data-testid="apps-suggestion" data-kind={proposal.kind} data-risk={action ? risk : undefined}>
      <header className="apps-suggestion__head">
        <span className="job__label">{t('apps.suggestion.title')}</span>
        {action ? (
          <Chip tone={riskTone(risk)} dot testId="apps-suggestion-risk">
            {t('apps.suggestion.risk', { level: t(`risk.${risk}` as MessageKey) })}
          </Chip>
        ) : null}
        {action && proposal.irreversible ? (
          <Chip tone="danger" testId="apps-suggestion-irreversible">
            {t('apps.suggestion.irreversible')}
          </Chip>
        ) : null}
      </header>

      {action ? (
        <>
          <p className="apps-suggestion__words" data-testid="apps-suggestion-words">
            <BidiText text={proposal.words ?? ''} />
          </p>
          {proposal.window || proposal.element ? (
            <dl className="kv">
              {proposal.window ? (
                <>
                  <dt>{t('apps.suggestion.where')}</dt>
                  <dd data-testid="apps-suggestion-where">
                    <BidiText text={proposal.window} />
                  </dd>
                </>
              ) : null}
              {proposal.element ? (
                <>
                  <dt>{t('apps.suggestion.element')}</dt>
                  <dd>
                    <BidiText text={proposal.element} />
                    {proposal.control ? (
                      <Chip tone="steel" className="apps-seen__control">
                        <bdi dir="ltr">{proposal.control}</bdi>
                      </Chip>
                    ) : null}
                  </dd>
                </>
              ) : null}
            </dl>
          ) : null}
          {proposal.preview ? (
            <div className="apps-preview" role="note" data-testid="apps-suggestion-preview">
              <Icon name="alert" size={14} />
              <BidiText text={proposal.preview} />
            </div>
          ) : null}
          {proposal.reasons?.length ? (
            <div className="apps-reasons">
              <span className="job__label">{t('apps.suggestion.why')}</span>
              <ul data-testid="apps-suggestion-reasons">
                {proposal.reasons.map((line, i) => (
                  <li key={i}>
                    <BidiText text={line} />
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {proposal.asks_again ? (
            <p className="hint" data-testid="apps-suggestion-again">
              <Icon name="shield" size={12} />
              {t('apps.suggestion.asksAgain')}
            </p>
          ) : null}
          {proposal.refused ? (
            <div className="job__error" role="alert" data-testid="apps-suggestion-refused">
              <Icon name="octagon" size={14} />
              <BidiText text={t('apps.suggestion.refused', { reason: proposal.refused })} />
            </div>
          ) : null}
        </>
      ) : (
        <div className="apps-suggestion__answer">
          <span className="job__label">{t('apps.suggestion.answer')}</span>
          <p data-testid="apps-suggestion-answer">
            <BidiText text={proposal.answer ?? ''} />
          </p>
        </div>
      )}
      {proposal.understood ? (
        <p className="hint apps-suggestion__understood">
          <strong>{t('apps.suggestion.understood')}: </strong>
          <BidiText text={proposal.understood} />
        </p>
      ) : null}

      <footer className="apps-suggestion__foot">
        {step ? (
          <StepState job={step} />
        ) : proposal.can_do ? (
          <>
            <GatedButton why={doWhy} hint={t('apps.suggestion.doHint')} pending={doing} onClick={() => onDo(id)} icon="pointer" testId="apps-do">
              {t('apps.suggestion.do')}
            </GatedButton>
            <button type="button" className="btn btn--sm btn--ghost" onClick={onDismiss} data-testid="apps-dismiss">
              {t('apps.suggestion.dismiss')}
            </button>
          </>
        ) : (
          <>
            <span className="hint" data-testid="apps-suggestion-nothing">
              {t('apps.suggestion.nothing')}
            </span>
            <button type="button" className="btn btn--sm btn--ghost" onClick={onDismiss} data-testid="apps-dismiss">
              {t('apps.suggestion.dismiss')}
            </button>
          </>
        )}
      </footer>
    </article>
  );
}

/** Where the chosen step is: waiting for the approval, being done, done, or not done and why. */
function StepState({ job }: { job: Job }) {
  const { t } = useI18n();
  const phase = job.steps.find((s) => s.status === 'running')?.key;
  let text: string;
  if (job.status === 'running') text = phase === 'act' ? t('apps.suggestion.doing') : t('apps.suggestion.waiting');
  else if (job.status === 'done') text = t('apps.suggestion.stepDone');
  else text = t('apps.suggestion.stepNot', { reason: job.error || job.message || t(`job.status.${job.status}` as MessageKey) });
  return (
    <div className="apps-step-state" data-testid="apps-step-state" data-status={job.status}>
      <StatusChip status={job.status} tone={JOB_TONE[job.status]} label={t(`job.status.${job.status}` as MessageKey)} />
      <BidiText text={text} />
    </div>
  );
}
