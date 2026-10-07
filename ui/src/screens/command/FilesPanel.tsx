import { useEffect, useState } from 'react';
import { MODES, type Mode, type RecentFile } from '../../api/types';
import { useI18n, type MessageKey } from '../../i18n';
import { formatBytes, formatRelative } from '../../lib/format';
import { useApi, useAppState, useNow, useResource, useRunner, useToast } from '../../state/context';
import { useSetMode } from '../../state/hooks';
import { Icon } from '../../components/Icon';
import { Panel, Segmented } from '../../components/ui';

const FILE_ICONS: Record<string, string> = { html: 'code', css: 'code', js: 'code', ts: 'code', py: 'code', json: 'code', md: 'file', txt: 'file', png: 'camera', jpg: 'camera', svg: 'camera', pdf: 'file' };

function fileIcon(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  return FILE_ICONS[ext] ?? 'file';
}

export function RecentFileRow({ file, now, onOpen }: { file: RecentFile; now: number; onOpen: (reveal: boolean) => void }) {
  const { t, lang } = useI18n();
  return (
    <li className="file-row" data-testid="recent-file">
      <button type="button" className="file-row__open" onClick={() => onOpen(false)} title={`${t('files.open')}: ${file.path}`}>
        <Icon name={fileIcon(file.name)} size={14} />
        <span className="file-row__name truncate ltr">{file.name}</span>
        <span className="file-row__meta">
          {file.size !== null ? (
            <>
              <span className="ltr">{formatBytes(file.size)}</span>
              {' · '}
            </>
          ) : null}
          {formatRelative(file.created_at, lang, now)}
        </span>
      </button>
      <button type="button" className="icon-btn icon-btn--sm" onClick={() => onOpen(true)} aria-label={t('files.reveal')} title={t('files.reveal')}>
        <Icon name="folder" size={13} />
      </button>
    </li>
  );
}

/** Guarded global kill switch: arm, confirm, then release when engaged. */
export function KillSwitchControl({ compact }: { compact?: boolean }) {
  const { t } = useI18n();
  const api = useApi();
  const engaged = useAppState((s) => s.killSwitch.global);
  const [arming, setArming] = useState(false);
  const { run, isPending } = useRunner(t('common.requestFailed'));
  useEffect(() => {
    if (!arming) return;
    const timer = setTimeout(() => setArming(false), 8000);
    return () => clearTimeout(timer);
  }, [arming]);

  if (engaged) {
    return (
      <div className="kill kill--engaged" data-testid="kill-switch" data-engaged="true">
        <span className="kill__state">
          <Icon name="octagon" size={16} />
          <span className="truncate">{t('kill.engaged')}</span>
        </span>
        <button type="button" className="btn btn--sm btn--ok" disabled={isPending('release')} onClick={() => void run('release', () => api.killSwitch(false))} data-testid="kill-release">
          {t('kill.release')}
        </button>
      </div>
    );
  }
  if (arming) {
    return (
      <div className="inline-confirm" role="alertdialog" aria-label={t('kill.confirmTitle')} data-testid="kill-confirm">
        <p className="inline-confirm__title">{t('kill.confirmTitle')}</p>
        {!compact ? <p className="inline-confirm__body">{t('kill.confirmBody')}</p> : null}
        <div className="inline-confirm__actions">
          <button
            type="button"
            className="btn btn--sm btn--danger-solid"
            disabled={isPending('engage')}
            onClick={() =>
              void run('engage', async () => {
                await api.killSwitch(true, undefined, t('kill.reason'));
                setArming(false);
              })
            }
            data-testid="kill-confirm-yes"
            autoFocus
          >
            {t('kill.confirm')}
          </button>
          <button type="button" className="btn btn--sm btn--ghost" onClick={() => setArming(false)}>
            {t('common.cancel')}
          </button>
        </div>
      </div>
    );
  }
  return (
    <button type="button" className="kill" onClick={() => setArming(true)} data-testid="kill-switch" data-engaged="false">
      <Icon name="octagon" size={17} />
      <span>{t('kill.button')}</span>
    </button>
  );
}

/** Files & System: folders, quick system actions, mode, recent files and the kill switch. */
export function FilesPanel({ className }: { className?: string }) {
  const { t } = useI18n();
  const api = useApi();
  const toast = useToast();
  const files = useAppState((s) => s.files);
  const mode = useAppState((s) => s.mode);
  const finished = useAppState((s) => s.tasks.recent.length);
  const { setMode, pending: modePending } = useSetMode();
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const now = useNow(30000);
  const recent = useResource((a) => a.getRecentFiles(12), [finished]);

  const open = (key: string, path: string, reveal = false) =>
    void run(key, async () => {
      await api.openFile(path, reveal);
    });
  const command = (key: string, text: string) =>
    void run(key, async () => {
      await api.command(text, 'text');
      toast('success', t('files.sent'), text);
    });

  const actions = [
    { key: 'workspace', icon: 'folder', label: t('files.workspace'), onClick: () => open('workspace', files.workspace), disabled: !files.workspace },
    { key: 'projects', icon: 'layers', label: t('files.projects'), onClick: () => open('projects', files.projects), disabled: !files.projects },
    { key: 'screenshot', icon: 'camera', label: t('files.screenshot'), onClick: () => command('screenshot', t('cmd.screenshot')), disabled: false },
    { key: 'lock', icon: 'lock', label: t('files.lock'), onClick: () => command('lock', t('cmd.lock')), disabled: false },
  ];

  return (
    <Panel className={`files-panel ${className ?? ''}`} title={t('files.title')} icon="folder" testId="panel-files" bodyClassName="files-panel__body">
      <div className="quick-actions">
        {actions.map((a) => (
          <button key={a.key} type="button" className="quick-action" onClick={a.onClick} disabled={a.disabled || isPending(a.key)} data-testid={`quick-${a.key}`} title={a.key === 'workspace' ? files.workspace : a.key === 'projects' ? files.projects : a.label}>
            <span className="quick-action__icon">{isPending(a.key) ? <span className="spinner spinner--xs" /> : <Icon name={a.icon} size={17} />}</span>
            <span className="quick-action__label truncate">{a.label}</span>
          </button>
        ))}
      </div>
      <div className="files-panel__mode">
        <Segmented<Mode>
          value={mode}
          onChange={(m) => void setMode(m)}
          label={t('files.mode')}
          options={MODES.map((m) => ({ value: m, label: t(`mode.short.${m}` as MessageKey), title: `${t(`mode.${m}` as MessageKey)} — ${t(`mode.desc.${m}` as MessageKey)}` }))}
          testId="mode-selector"
          disabled={modePending}
        />
      </div>
      <div className="files-panel__recent">
        <p className="section-label">{t('files.recent')}</p>
        {recent.data?.length ? (
          <ul className="file-list" data-testid="recent-files">
            {recent.data.map((file) => (
              <RecentFileRow key={file.path} file={file} now={now} onOpen={(reveal) => open(`file:${file.path}`, file.path, reveal)} />
            ))}
          </ul>
        ) : recent.loading ? (
          <div className="skeleton" style={{ height: 22 }} />
        ) : recent.error ? (
          <p className="hint">{recent.error}</p>
        ) : (
          <p className="hint">{t('files.noRecent')}</p>
        )}
      </div>
      <KillSwitchControl compact />
    </Panel>
  );
}
