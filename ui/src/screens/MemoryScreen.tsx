import { useState } from 'react';
import { FACT_CATEGORIES, type FactCategory, type MemoryFact, type MemoryProject, type MemorySearchHit } from '../api/types';
import { useI18n, type MessageKey } from '../i18n';
import { formatRelative, textDir } from '../lib/format';
import { useDebounced } from '../lib/hooks';
import type { ScreenId } from '../lib/router';
import { useApi, useAppState, useNow, useResource, useRunner, useToast } from '../state/context';
import { Icon } from '../components/Icon';
import { ScreenHeader } from '../components/shell/Shell';
import type { Tone } from '../components/tones';
import { Chip, ConfirmButton, EmptyState, ErrorBox, Loading, Panel } from '../components/ui';
import { RecentFileRow } from './command/FilesPanel';

type Navigate = (screen: ScreenId, param?: string | null) => void;

const LAYER_TONE: Record<string, Tone> = {
  long_term: 'think',
  short_term: 'info',
  conversation: 'info',
  project: 'ok',
  task: 'work',
  file: 'verify',
  command: 'steel',
};

const CATEGORY_TONE: Record<string, Tone> = {
  preference: 'think',
  fact: 'info',
  decision: 'warn',
  note: 'steel',
  person: 'ok',
  project: 'work',
};

/** The translation of `key`, or `fallback` for values the dictionary does not know (free-form data). */
function labelOr(t: (key: MessageKey) => string, key: string, fallback: string): string {
  const text = t(key as MessageKey);
  return text === key ? fallback : text;
}

function categoryLabel(t: (key: MessageKey) => string, category: string): string {
  return labelOr(t, `memory.cat.${category}`, category);
}

function SearchPanel({ onNavigate }: { onNavigate: Navigate }) {
  const { t, lang } = useI18n();
  const [query, setQuery] = useState('');
  const q = useDebounced(query.trim(), 300);
  const now = useNow(60000);
  const results = useResource<MemorySearchHit[] | null>((api) => (q ? api.searchMemory(q, 30) : Promise.resolve(null)), [q]);
  const hits = results.data ?? null;
  const open = (hit: MemorySearchHit) => {
    if (hit.layer === 'task' && !hit.id.startsWith('result:')) onNavigate('tasks', hit.id);
  };
  return (
    <Panel className="memory-search" title={t('memory.search')} icon="search" testId="memory-search">
      <label className="search search--lg">
        <Icon name="search" size={16} />
        <input className="input" type="search" value={query} placeholder={t('memory.searchPlaceholder')} aria-label={t('memory.search')} onChange={(e) => setQuery(e.target.value)} data-testid="memory-search-input" />
      </label>
      {!q ? (
        <p className="hint memory-search__hint">{t('memory.searchHint')}</p>
      ) : results.loading && !hits ? (
        <Loading label={t('common.loading')} rows={3} />
      ) : results.error ? (
        <ErrorBox message={results.error} onRetry={results.reload} retryLabel={t('common.retry')} />
      ) : hits && hits.length ? (
        <ol className="hits" data-testid="memory-hits">
          {hits.map((hit) => {
            const linked = hit.layer === 'task' && !hit.id.startsWith('result:');
            return (
              <li key={`${hit.layer}:${hit.id}`} className="hit">
                <Chip tone={LAYER_TONE[hit.layer] ?? 'steel'}>{t(`memory.layer.${hit.layer}` as MessageKey)}</Chip>
                {linked ? (
                  <button type="button" className="hit__text link-btn bidi" dir={textDir(hit.text)} onClick={() => open(hit)}>
                    {hit.text}
                  </button>
                ) : (
                  <span className="hit__text bidi" dir={textDir(hit.text)}>
                    {hit.text}
                  </span>
                )}
                <span className="hit__time">{formatRelative(hit.ts, lang, now)}</span>
              </li>
            );
          })}
        </ol>
      ) : (
        <EmptyState icon="search" title={t('memory.noResults')} />
      )}
    </Panel>
  );
}

function FactRow({ fact, now, onDelete, deleting }: { fact: MemoryFact; now: number; onDelete: () => void; deleting: boolean }) {
  const { t, lang } = useI18n();
  return (
    <li className="fact" data-testid="memory-fact">
      <Chip tone={CATEGORY_TONE[fact.category] ?? 'steel'}>{categoryLabel(t, fact.category)}</Chip>
      <span className="fact__text bidi" dir={textDir(fact.text)}>
        {fact.text}
      </span>
      <span className="fact__meta">{formatRelative(fact.updated_at ?? fact.created_at, lang, now)}</span>
      <ConfirmButton className="icon-btn icon-btn--sm icon-btn--danger" confirmLabel={t('common.delete')} title={t('memory.deleteConfirm')} onConfirm={onDelete} disabled={deleting} testId="memory-fact-delete">
        <Icon name="trash" size={13} />
      </ConfirmButton>
    </li>
  );
}

function FactsPanel() {
  const { t } = useI18n();
  const api = useApi();
  const toast = useToast();
  const [filter, setFilter] = useState('');
  const q = useDebounced(filter.trim(), 250);
  const [text, setText] = useState('');
  const [category, setCategory] = useState<FactCategory>('fact');
  const facts = useResource((a) => a.getFacts(q || undefined), [q]);
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const now = useNow(60000);

  const add = () => {
    const value = text.trim();
    if (!value) return;
    void run('add', async () => {
      const fact = await api.addFact(value, category);
      setText('');
      facts.setData((prev) => (prev ? [fact, ...prev.filter((f) => f.id !== fact.id)] : [fact]));
      toast('success', t('memory.added'), value);
    });
  };
  const remove = (fact: MemoryFact) =>
    void run(`del:${fact.id}`, async () => {
      await api.deleteFact(fact.id);
      facts.setData((prev) => prev?.filter((f) => f.id !== fact.id));
    });

  return (
    <Panel className="memory-facts" title={t('memory.facts')} icon="memory" meta={facts.data ? <span className="num">{facts.data.length}</span> : null} flush testId="memory-facts">
      <form
        className="fact-form"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <input className="input" value={text} dir={text.trim() ? textDir(text) : undefined} placeholder={t('memory.factPlaceholder')} aria-label={t('memory.addFact')} onChange={(e) => setText(e.target.value)} maxLength={2000} data-testid="memory-fact-input" />
        <select className="select" value={category} onChange={(e) => setCategory(e.target.value as FactCategory)} aria-label={t('memory.category')} data-testid="memory-fact-category">
          {FACT_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {categoryLabel(t, c)}
            </option>
          ))}
        </select>
        <button type="submit" className="btn btn--primary" disabled={!text.trim() || isPending('add')} data-testid="memory-fact-add">
          {isPending('add') ? <span className="spinner spinner--xs" /> : <Icon name="plus" size={14} />}
          {t('memory.addFact')}
        </button>
      </form>
      <div className="fact-filter">
        <label className="search">
          <Icon name="filter" size={13} />
          <input className="input" type="search" value={filter} placeholder={t('memory.filterFacts')} aria-label={t('memory.filterFacts')} onChange={(e) => setFilter(e.target.value)} />
        </label>
      </div>
      {facts.error ? (
        <div className="panel-pad">
          <ErrorBox message={facts.error} onRetry={facts.reload} retryLabel={t('common.retry')} />
        </div>
      ) : facts.loading && !facts.data ? (
        <div className="panel-pad">
          <Loading label={t('common.loading')} rows={4} />
        </div>
      ) : facts.data?.length ? (
        <ul className="facts">
          {facts.data.map((fact) => (
            <FactRow key={fact.id} fact={fact} now={now} onDelete={() => remove(fact)} deleting={isPending(`del:${fact.id}`)} />
          ))}
        </ul>
      ) : (
        <EmptyState icon="memory" title={t('memory.noFacts')} hint={t('memory.factsHint')} />
      )}
    </Panel>
  );
}

function ProjectRow({ project, now, onOpen }: { project: MemoryProject; now: number; onOpen: (path: string) => void }) {
  const { t, lang } = useI18n();
  return (
    <li className="project" data-testid="memory-project">
      <span className="project__icon">
        <Icon name="layers" size={16} />
      </span>
      <span className="project__main">
        <span className="project__name bidi">{project.name}</span>
        {project.description ? <span className="project__desc bidi">{project.description}</span> : null}
        {project.path ? <span className="project__path ltr truncate">{project.path}</span> : null}
      </span>
      <span className="project__side">
        <Chip tone={project.status === 'active' ? 'ok' : 'muted'}>{labelOr(t, `memory.projectStatus.${project.status}`, project.status)}</Chip>
        <span className="faint">{formatRelative(project.updated_at, lang, now)}</span>
        {project.path ? (
          <button type="button" className="icon-btn icon-btn--sm" onClick={() => onOpen(project.path!)} aria-label={t('memory.openFolder')} title={t('memory.openFolder')}>
            <Icon name="folder" size={13} />
          </button>
        ) : null}
      </span>
    </li>
  );
}

function ProjectsPanel() {
  const { t } = useI18n();
  const api = useApi();
  const projects = useResource((a) => a.getProjects(), []);
  const { run } = useRunner(t('common.requestFailed'));
  const now = useNow(60000);
  return (
    <Panel
      className="memory-projects"
      title={t('memory.projects')}
      icon="layers"
      meta={projects.data ? <span className="num">{projects.data.length}</span> : null}
      actions={
        <button type="button" className="icon-btn icon-btn--sm" onClick={projects.reload} aria-label={t('common.refresh')} title={t('common.refresh')}>
          <Icon name="restart" size={13} />
        </button>
      }
      testId="memory-projects"
    >
      {projects.error ? (
        <ErrorBox message={projects.error} onRetry={projects.reload} retryLabel={t('common.retry')} />
      ) : projects.loading && !projects.data ? (
        <Loading label={t('common.loading')} rows={3} />
      ) : projects.data?.length ? (
        <ul className="projects">
          {projects.data.map((p) => (
            <ProjectRow key={p.id} project={p} now={now} onOpen={(path) => void run(`open:${p.id}`, () => api.openFile(path, false))} />
          ))}
        </ul>
      ) : (
        <EmptyState icon="layers" title={t('memory.noProjects')} hint={t('memory.projectsHint')} />
      )}
    </Panel>
  );
}

function FilesPanel() {
  const { t } = useI18n();
  const api = useApi();
  const finished = useAppState((s) => s.tasks.recent.length);
  const files = useResource((a) => a.getRecentFiles(40), [finished]);
  const { run } = useRunner(t('common.requestFailed'));
  const now = useNow(60000);
  return (
    <Panel className="memory-files" title={t('files.recent')} icon="file" meta={files.data ? <span className="num">{files.data.length}</span> : null} testId="memory-files">
      {files.error ? (
        <ErrorBox message={files.error} onRetry={files.reload} retryLabel={t('common.retry')} />
      ) : files.loading && !files.data ? (
        <Loading label={t('common.loading')} rows={3} />
      ) : files.data?.length ? (
        <ul className="file-list">
          {files.data.map((file) => (
            <RecentFileRow key={file.path} file={file} now={now} onOpen={(reveal) => void run(`file:${file.path}`, () => api.openFile(file.path, reveal))} />
          ))}
        </ul>
      ) : (
        <EmptyState icon="file" title={t('files.noRecent')} />
      )}
    </Panel>
  );
}

/** What JARVIS remembers: search every layer, edit long-term facts, projects and the files it made. */
export function MemoryScreen({ onNavigate }: { onNavigate: Navigate }) {
  const { t } = useI18n();
  return (
    <div className="screen screen--memory" data-testid="memory-screen">
      <ScreenHeader title={t('memory.title')} subtitle={t('memory.subtitle')} icon="memory" />
      <div className="memory-layout">
        <SearchPanel onNavigate={onNavigate} />
        <FactsPanel />
        <ProjectsPanel />
        <FilesPanel />
      </div>
    </div>
  );
}
