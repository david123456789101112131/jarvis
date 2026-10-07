import { useMemo } from 'react';
import type { SubTaskView, TaskSummary } from '../../api/types';
import { useI18n } from '../../i18n';
import { durationBetween, formatDuration, formatRelative } from '../../lib/format';
import type { ScreenId } from '../../lib/router';
import { useApi, useAppState, useDispatch, useNow, useRunner } from '../../state/context';
import { useEnsureTaskDetails } from '../../state/hooks';
import { sortActiveTasks, sortRecentTasks } from '../../state/selectors';
import { Icon } from '../../components/Icon';
import { SubtaskRow, TaskStatusChip } from '../../components/tasks';
import { TASK_TONE } from '../../components/tones';
import { ConfirmButton, EmptyState, Panel, ProgressBar } from '../../components/ui';

const EMPTY: SubTaskView[] = [];

function TaskCard({ task, subtasks, onOpen }: { task: TaskSummary; subtasks: SubTaskView[]; onOpen: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const dispatch = useDispatch();
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const done = subtasks.filter((s) => s.status === 'completed' || s.status === 'skipped').length;
  const planning = task.status === 'planning' || task.status === 'pending';
  const cancel = () =>
    void run('cancel', async () => {
      const updated = await api.cancelTask(task.id);
      if (updated?.id) dispatch({ type: 'event', event: { type: 'task.updated', data: updated }, at: Date.now() });
    });
  return (
    <article className="task-card" data-status={task.status} data-testid="task-item" data-task-id={task.id}>
      <header className="task-card__head">
        <TaskStatusChip status={task.status} />
        <button type="button" className="task-card__title truncate bidi" onClick={onOpen} title={task.title}>
          {task.title}
        </button>
        <span className="task-card__pct num">{Math.round(task.progress * 100)}%</span>
        <ConfirmButton className="icon-btn icon-btn--sm icon-btn--danger" confirmLabel={t('common.confirm')} onConfirm={cancel} disabled={isPending('cancel')} title={t('tasks.cancel')} testId="task-cancel">
          <Icon name="x" size={13} />
        </ConfirmButton>
      </header>
      <ProgressBar value={task.progress} tone={TASK_TONE[task.status]} indeterminate={planning && task.progress === 0} label={task.title} />
      {subtasks.length ? (
        <>
          <ul className="subtasks">
            {subtasks.map((s) => (
              <SubtaskRow key={s.id} subtask={s} />
            ))}
          </ul>
          <p className="task-card__foot">
            <span>{t('tasks.subtasks', { done, total: subtasks.length })}</span>
            <span className="task-card__agents">{task.priority !== 'normal' ? t(`priority.${task.priority}`) : null}</span>
          </p>
        </>
      ) : planning ? (
        <p className="task-card__planning">
          <span className="spinner spinner--xs" aria-hidden="true" />
          {t('tasks.planning')}
        </p>
      ) : null}
    </article>
  );
}

function RecentRow({ task, now, onOpen }: { task: TaskSummary; now: number; onOpen: () => void }) {
  const { lang } = useI18n();
  const duration = durationBetween(task.started_at ?? task.created_at, task.finished_at ?? task.updated_at, now);
  return (
    <li>
      <button type="button" className="recent-row" data-status={task.status} onClick={onOpen} data-testid="recent-task">
        <span className="recent-row__dot" data-tone={TASK_TONE[task.status]} aria-hidden="true" />
        <span className="recent-row__title truncate bidi">{task.title}</span>
        <span className="recent-row__meta">
          {duration !== null ? <span className="num">{formatDuration(duration, lang)}</span> : null}
          <span>{formatRelative(task.finished_at ?? task.updated_at, lang, now)}</span>
        </span>
      </button>
    </li>
  );
}

export function TaskPanel({ className, onNavigate }: { className?: string; onNavigate: (screen: ScreenId, param?: string | null) => void }) {
  const { t } = useI18n();
  const activeRaw = useAppState((s) => s.tasks.active);
  const recentRaw = useAppState((s) => s.tasks.recent);
  const details = useAppState((s) => s.taskDetails);
  const live = useAppState((s) => s.liveSubtasks);
  const now = useNow(20000);
  const active = useMemo(() => sortActiveTasks(activeRaw), [activeRaw]);
  const recent = useMemo(() => sortRecentTasks(recentRaw).slice(0, 8), [recentRaw]);
  useEnsureTaskDetails(useMemo(() => active.map((task) => task.id), [active]));

  return (
    <Panel
      className={className}
      title={t('tasks.title')}
      icon="tasks"
      meta={active.length ? <span className="num">{t('core.activeTasks', { n: active.length })}</span> : null}
      actions={
        <button type="button" className="icon-btn icon-btn--sm" onClick={() => onNavigate('tasks')} aria-label={t('nav.tasks')} title={t('nav.tasks')}>
          <Icon name="external" size={14} />
        </button>
      }
      testId="panel-tasks"
      bodyClassName="task-panel__body"
    >
      {active.length ? (
        <div className="task-panel__active">
          {active.map((task) => (
            <TaskCard key={task.id} task={task} subtasks={details[task.id]?.plan?.subtasks ?? live[task.id] ?? EMPTY} onOpen={() => onNavigate('tasks', task.id)} />
          ))}
        </div>
      ) : (
        <EmptyState icon="tasks" title={t('tasks.empty')} hint={t('tasks.emptyHint')} testId="tasks-empty" />
      )}
      <p className="section-label task-panel__recent-label">{t('tasks.recent')}</p>
      {recent.length ? (
        <ul className="recent-list">
          {recent.map((task) => (
            <RecentRow key={task.id} task={task} now={now} onOpen={() => onNavigate('tasks', task.id)} />
          ))}
        </ul>
      ) : (
        <p className="hint">{t('tasks.noRecent')}</p>
      )}
    </Panel>
  );
}
