import type { SubTaskView, TaskStatus } from '../api/types';
import { useI18n, type MessageKey } from '../i18n';
import { Icon } from './Icon';
import { TASK_TONE } from './tones';
import { AgentBadge, StatusChip } from './ui';

export const AGENT_NUMBERS: Record<string, number> = { research: 1, system: 2, development: 3, creative: 4, planner: 5 };

export function TaskStatusChip({ status, size }: { status: TaskStatus; size?: 'lg' }) {
  const { t } = useI18n();
  return <StatusChip status={status} tone={TASK_TONE[status]} label={t(`taskStatus.${status}` as MessageKey)} size={size} />;
}

/** Small status glyph for a subtask: check, cross, spinner/percent, dot. */
export function SubtaskStateIcon({ subtask }: { subtask: Pick<SubTaskView, 'status' | 'progress'> }) {
  const { t } = useI18n();
  const label = t(`taskStatus.${subtask.status}` as MessageKey);
  switch (subtask.status) {
    case 'completed':
      return (
        <span className="sub-state" data-tone="ok" title={label}>
          <Icon name="check" size={13} strokeWidth={2.4} />
        </span>
      );
    case 'failed':
      return (
        <span className="sub-state" data-tone="danger" title={label}>
          <Icon name="x" size={13} strokeWidth={2.4} />
        </span>
      );
    case 'running':
    case 'verifying':
      return (
        <span className="sub-state sub-state--pct num" data-tone={TASK_TONE[subtask.status]} title={label}>
          {Math.round((subtask.progress || 0) * 100)}%
        </span>
      );
    case 'waiting':
      return (
        <span className="sub-state" data-tone="warn" title={label}>
          <Icon name="pause" size={12} />
        </span>
      );
    case 'cancelled':
    case 'skipped':
      return (
        <span className="sub-state" data-tone="muted" title={label}>
          <Icon name="minus" size={12} />
        </span>
      );
    case 'interrupted':
      return (
        <span className="sub-state" data-tone="orange" title={label}>
          <Icon name="alert" size={12} />
        </span>
      );
    default:
      return <span className="sub-state sub-state--dot" data-tone="muted" title={label} />;
  }
}

export function SubtaskRow({ subtask }: { subtask: SubTaskView }) {
  const { agentName } = useI18n();
  return (
    <li className="subtask" data-status={subtask.status}>
      <span title={agentName(subtask.agent)}>
        <AgentBadge agent={subtask.agent} number={AGENT_NUMBERS[subtask.agent] ?? '•'} size="sm" />
      </span>
      <span className="subtask__title truncate bidi" title={subtask.title}>
        {subtask.title}
      </span>
      <SubtaskStateIcon subtask={subtask} />
    </li>
  );
}
