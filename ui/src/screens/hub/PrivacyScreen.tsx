import { privacy, type PrivacyCleared, type PrivacyInventory, type PrivacyItem, type PrivacyKey, type PrivacyKept } from '../../api/privacy';
import { Icon } from '../../components/Icon';
import { ScreenHeader } from '../../components/shell/Shell';
import { ConfirmButton, ErrorBox, Loading, Panel } from '../../components/ui';
import { Unavailable } from '../../components/work/JobCard';
import { useI18n, type MessageKey } from '../../i18n';
import { formatBytes } from '../../lib/format';
import { useApi, useAppState, useDispatch, useResource, useRunner, useServices, useToast } from '../../state/context';

/** What JARVIS keeps on this computer, and deleting it; and what can reach the microphone, the screen and the internet. */
export function PrivacyScreen() {
  const { t } = useI18n();
  const { boot } = useServices();
  if (boot.mock)
    return (
      <div className="area" data-testid="privacy-screen">
        <ScreenHeader title={t('privacy.title')} subtitle={t('privacy.subtitle')} icon="lock" />
        <Unavailable reason={t('unavailable.simulation')} testId="privacy-simulation" />
      </div>
    );
  return <PrivacyLive />;
}

function PrivacyLive() {
  const { t } = useI18n();
  const api = useApi();
  const toast = useToast();
  const dispatch = useDispatch();
  const inventory = useResource((a) => privacy.inventory(a), []);
  const { run, isPending } = useRunner(t('job.actionFailed'));
  const data = inventory.data;
  const ready = (data?.items ?? []).filter((i) => i.count > 0 && !i.busy).map((i) => i.key);
  const name = (key: PrivacyKey) => t(`privacy.item.${key}` as MessageKey);

  const report = (out: PrivacyCleared, keys: PrivacyKey[]) => {
    const done = keys.filter((k) => out.results[k]?.ok);
    const failed = keys.filter((k) => out.results[k] && !out.results[k]?.ok);
    if (done.length) toast('success', t('privacy.cleared', { names: done.map(name).join(', ') }));
    else if (!failed.length) toast('info', t('privacy.clearedNone'));
    if (failed.length) {
      const why = (k: PrivacyKey) => t(out.results[k]?.reason === 'busy' ? 'privacy.notClearedBusy' : 'privacy.notClearedError');
      toast('warning', t('privacy.notCleared', { names: failed.map((k) => `${name(k)} (${why(k)})`).join(', ') }));
    }
    if (out.results.logs?.in_use?.length) toast('info', t('privacy.logsInUse'));
  };

  const clear = (keys: PrivacyKey[]) =>
    void run(keys.length > 1 ? 'clear:all' : `clear:${keys[0]}`, () => privacy.clear(api, keys)).then((out) => {
      if (!out) return;
      inventory.setData(() => out.inventory);
      if (out.results.chat?.ok) dispatch({ type: 'chat.cleared' });
      report(out, keys);
    });

  return (
    <div className="area" data-testid="privacy-screen">
      <ScreenHeader
        title={t('privacy.title')}
        subtitle={t('privacy.subtitle')}
        icon="lock"
        actions={
          ready.length ? (
            <ConfirmButton className="btn btn--sm" confirmLabel={t('privacy.clearAllConfirm')} onConfirm={() => clear(ready)} disabled={isPending('clear:all')} testId="privacy-clear-all">
              <Icon name="trash" size={13} /> {t('privacy.clearAll')}
            </ConfirmButton>
          ) : null
        }
      />
      {inventory.error ? <ErrorBox message={inventory.error} onRetry={inventory.reload} /> : null}
      {!data && inventory.loading ? <Loading label={t('privacy.loading')} rows={6} /> : null}
      {data ? (
        <div className="privacy__cols">
          <Panel title={t('privacy.kept')} icon="disk" testId="privacy-items-panel">
            <p className="field__hint">{t('privacy.keptHint')}</p>
            <ul className="privacy-list" data-testid="privacy-items">
              {data.items.map((item) => (
                <PrivacyRow key={item.key} item={item} pending={isPending(`clear:${item.key}`) || isPending('clear:all')} onClear={() => clear([item.key])} />
              ))}
            </ul>
            <p className="field__hint num" data-testid="privacy-database">
              {t('privacy.database', { size: formatBytes(data.database.bytes) })}
            </p>
          </Panel>
          <div className="area__stack">
            <Senses data={data} />
            <KeptOnPurpose kept={data.kept} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

function PrivacyRow({ item, pending, onClear }: { item: PrivacyItem; pending: boolean; onClear: () => void }) {
  const { t } = useI18n();
  const empty = item.count === 0;
  return (
    <li className="privacy-row" data-testid={`privacy-item-${item.key}`} data-empty={empty || undefined} data-busy={item.busy || undefined}>
      <div className="privacy-row__text">
        <span className="privacy-row__name">{t(`privacy.item.${item.key}` as MessageKey)}</span>
        <span className="privacy-row__hint">{t(`privacy.item.${item.key}.hint` as MessageKey)}</span>
        <span className="privacy-row__meta">
          <span className="num" data-testid={`privacy-count-${item.key}`}>
            {empty ? t('privacy.nothing') : t(`privacy.count.${item.key}` as MessageKey, { n: item.count })}
          </span>
          {item.bytes ? <span className="num">{formatBytes(item.bytes)}</span> : null}
          {item.key === 'tasks' && item.kept ? <span>{t('privacy.stay.tasks', { n: item.kept })}</span> : null}
          {item.key === 'jobs' && item.running ? <span>{t('privacy.stay.jobs', { n: item.running })}</span> : null}
        </span>
        {item.busy ? (
          <span className="privacy-row__busy" role="status">
            <Icon name="clock" size={12} /> {t('privacy.busy')}
          </span>
        ) : null}
        {item.where.length ? (
          <details className="privacy-row__where">
            <summary>{t('privacy.where')}</summary>
            {item.where.map((w) => (
              <code key={w} className="mono ltr" dir="ltr">
                {w}
              </code>
            ))}
          </details>
        ) : null}
      </div>
      <ConfirmButton confirmLabel={t('privacy.clearConfirm')} onConfirm={onClear} disabled={empty || item.busy || pending} testId={`privacy-clear-${item.key}`}>
        <Icon name="trash" size={13} /> {t('privacy.clear')}
      </ConfirmButton>
    </li>
  );
}

function Senses({ data }: { data: PrivacyInventory }) {
  const { t } = useI18n();
  const api = useApi();
  const voice = useAppState((s) => s.voice);
  const { run, isPending } = useRunner(t('common.requestFailed'));
  const mic = data.senses.microphone;
  const { screen, cloud } = data.senses;
  const enabled = voice.enabled;
  const muted = voice.muted;
  const ctrl = mic.ctrl_to_talk ? t('privacy.mic.pushCtrl') : '';
  const keys = cloud.ai.filter((a) => a.configured).map((a) => a.name);

  let micLine: string;
  if (!enabled) micLine = t('privacy.mic.off');
  else if (muted) micLine = t('privacy.mic.muted');
  else if (mic.always_listening && mic.wake_word) micLine = t('privacy.mic.wake', { wake: mic.wake_word });
  else micLine = t('privacy.mic.push', { ctrl });

  return (
    <Panel title={t('privacy.senses')} icon="eye" testId="privacy-senses">
      <section className="privacy-sense" data-testid="privacy-mic">
        <h4 className="privacy-sense__title">
          <Icon name="mic" size={14} /> {t('privacy.mic')}
        </h4>
        <p data-testid="privacy-mic-state">{micLine}</p>
        {enabled ? <p>{t('privacy.mic.command', { ctrl })}</p> : null}
        {enabled ? <p data-testid="privacy-stt">{t(mic.stt_online ? 'privacy.mic.sttOnline' : 'privacy.mic.sttLocal')}</p> : null}
        {enabled ? (
          <p data-testid="privacy-tts" data-online={mic.tts_online || undefined}>
            {mic.tts === 'none' ? t('privacy.mic.ttsNone') : t(mic.tts_online ? 'privacy.mic.ttsOnline' : 'privacy.mic.ttsLocal')}
          </p>
        ) : null}
        <div className="actions-row">
          {enabled ? (
            <button type="button" className="btn btn--sm" disabled={isPending('mute')} onClick={() => void run('mute', () => api.voice(muted ? 'unmute' : 'mute'))} data-testid="privacy-mute">
              <Icon name={muted ? 'mic' : 'x'} size={13} /> {muted ? t('privacy.unmute') : t('privacy.mute')}
            </button>
          ) : null}
          <a className="btn btn--sm btn--ghost" href="#/settings">
            <Icon name="settings" size={13} /> {t('privacy.toSettings')}
          </a>
        </div>
      </section>
      <section className="privacy-sense" data-testid="privacy-screen-use">
        <h4 className="privacy-sense__title">
          <Icon name="monitor" size={14} /> {t('privacy.screen')}
        </h4>
        {screen.computer_enabled ? (
          <>
            <p>{t('privacy.screen.how')}</p>
            <p data-testid="privacy-screen-model">{screen.model === 'remote' ? t('privacy.screen.remote', { host: screen.remote_host ?? '' }) : t('privacy.screen.local')}</p>
          </>
        ) : (
          <p>{t('privacy.screen.off')}</p>
        )}
      </section>
      <section className="privacy-sense" data-testid="privacy-cloud">
        <h4 className="privacy-sense__title">
          <Icon name="cloud" size={14} /> {t('privacy.cloud')}
        </h4>
        {cloud.paid === false ? (
          <p data-testid="privacy-cloud-paid-off">{t('privacy.cloud.paidOff')}</p>
        ) : (
          <>
            <p>{t(cloud.only_when_told ? 'privacy.cloud.told' : 'privacy.cloud.notTold')}</p>
            <p data-testid="privacy-cloud-keys">{keys.length ? t('privacy.cloud.keys', { names: keys.join(', ') }) : t('privacy.cloud.noKeys')}</p>
          </>
        )}
        <p>{t(cloud.media === 'xai' ? 'privacy.cloud.media' : 'privacy.cloud.mediaLocal')}</p>
      </section>
    </Panel>
  );
}

function KeptOnPurpose({ kept }: { kept: PrivacyKept[] }) {
  const { t } = useI18n();
  const source = (s: string) => {
    const key = `privacy.source.${s}` as MessageKey;
    const label = t(key);
    return label === key ? s : label;
  };
  return (
    <Panel title={t('privacy.keptOnPurpose')} icon="shield" testId="privacy-kept">
      <ul className="privacy-kept">
        {kept.map((k) => (
          <li key={k.key} data-testid={`privacy-kept-${k.key}`}>
            <span className="privacy-row__name">{t(`privacy.keep.${k.key}` as MessageKey)}</span>
            <span className="privacy-row__hint">{t(`privacy.keep.${k.key}.hint` as MessageKey)}</span>
            {k.key === 'keys' ? (
              <span className="privacy-row__meta">{k.names?.length ? `${k.names.join(', ')} · ${k.where.map(source).join(', ')}` : t('privacy.keep.keys.none')}</span>
            ) : (
              k.where.map((w) => (
                <code key={w} className="mono ltr" dir="ltr">
                  {w}
                </code>
              ))
            )}
          </li>
        ))}
      </ul>
      <p className="field__hint">{t('privacy.oneByOne')}</p>
      <div className="actions-row">
        <a className="btn btn--sm btn--ghost" href="#/memory">
          <Icon name="memory" size={13} /> {t('privacy.toMemory')}
        </a>
        <a className="btn btn--sm btn--ghost" href="#/security">
          <Icon name="security" size={13} /> {t('privacy.toSecurity')}
        </a>
      </div>
    </Panel>
  );
}
