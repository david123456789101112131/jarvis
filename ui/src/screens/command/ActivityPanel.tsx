import { useEffect, useMemo, useRef, useState } from 'react';
import { AGENT_IDS, type ActivityEntry, type AgentId } from '../../api/types';
import { useI18n } from '../../i18n';
import { textDir } from '../../lib/format';
import { useAppState } from '../../state/context';
import { useStickToBottom } from '../../state/hooks';
import { filterActivity } from '../../state/selectors';
import { Icon } from '../../components/Icon';
import { Chip, EmptyState, Panel } from '../../components/ui';
import { BidiText } from '../../components/BidiText';

type Filter = AgentId | 'jarvis' | 'all';
const VISIBLE = 250;

export function ActivityLine({ entry }: { entry: ActivityEntry }) {
  return (
    <li className="log__line" data-level={entry.level} data-agent={entry.agent_id ?? 'jarvis'} data-testid="activity-line">
      <span className="log__time ltr">[{entry.time}]</span>
      <span className="log__msg" dir={textDir(entry.message)}>
        <BidiText text={entry.message} />
      </span>
    </li>
  );
}

/** Live activity log: auto-scrolls, pauses while hovered, filter by agent. */
export function ActivityPanel({ className }: { className?: string }) {
  const { t, agentName } = useI18n();
  const activity = useAppState((s) => s.activity);
  const [filter, setFilter] = useState<Filter>('all');
  const [hover, setHover] = useState(false);
  const entries = useMemo(() => filterActivity(activity, filter).slice(-VISIBLE), [activity, filter]);
  const lastId = entries.length ? entries[entries.length - 1].id : '';
  const { ref, onScroll, scrollToBottom, pinned } = useStickToBottom<HTMLOListElement>([lastId], hover);
  const [unseen, setUnseen] = useState(0);
  const seenId = useRef(lastId);
  const paused = hover || !pinned;

  useEffect(() => {
    if (!paused) {
      seenId.current = lastId;
      setUnseen(0);
      return;
    }
    const index = entries.findIndex((e) => e.id === seenId.current);
    setUnseen(index >= 0 ? entries.length - 1 - index : Math.min(entries.length, 99));
  }, [lastId, paused, entries]);

  return (
    <Panel
      className={className}
      title={t('activity.title')}
      icon="history"
      meta={
        <Chip tone={paused ? 'warn' : 'ok'} dot pulse={!paused} testId="activity-state">
          {paused ? t('activity.paused') : t('activity.live')}
        </Chip>
      }
      actions={
        <label className="mini-select">
          <span className="sr-only">{t('activity.filter')}</span>
          <Icon name="filter" size={13} />
          <select className="select select--sm" value={filter} onChange={(e) => setFilter(e.target.value as Filter)} data-testid="activity-filter">
            <option value="all">{t('activity.all')}</option>
            <option value="jarvis">JARVIS</option>
            {AGENT_IDS.map((id) => (
              <option key={id} value={id}>
                {agentName(id)}
              </option>
            ))}
          </select>
        </label>
      }
      testId="panel-activity"
      flush
    >
      <div className="log-wrap">
        {entries.length ? (
          <ol className="log" ref={ref} onScroll={onScroll} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)} aria-live="off">
            {entries.map((entry) => (
              <ActivityLine key={entry.id} entry={entry} />
            ))}
          </ol>
        ) : (
          <EmptyState icon="history" title={t('activity.empty')} />
        )}
        {paused && unseen > 0 ? (
          <button
            type="button"
            className="log__new"
            onClick={() => {
              setHover(false);
              scrollToBottom();
              setUnseen(0);
            }}
          >
            <Icon name="arrow-down" size={12} />
            {t('activity.newLines', { n: unseen })}
          </button>
        ) : null}
      </div>
    </Panel>
  );
}
