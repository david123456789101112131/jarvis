import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { ApiError } from '../../api/client';
import { EXTENSION, canCreate, isLocal, jobItems, media, missingFor, usd, type MediaItem, type MediaKind, type MediaStatus } from '../../api/media';
import type { Job } from '../../api/work';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { Chip, ConfirmButton, EmptyState } from '../../components/ui';
import { JobCard, Unavailable } from '../../components/work/JobCard';
import { PathPicker } from '../../components/work/PathPicker';
import { useI18n } from '../../i18n';
import { formatBytes, hasHebrew } from '../../lib/format';
import { errorMessage, useApi, useAppState, useResource, useRunner, useServices, useStore, useToast } from '../../state/context';

// What the image and video screens share: the engine's status (the free models on this computer, or xAI when the
// settings choose it), the box that says what the model gets and what leaves the computer, the previews, saving and
// deleting a preview, and the screen's own job cards.

export type MediaArea = 'images' | 'video';

export const IMAGE_FILE_TYPES = ['Images (*.png;*.jpg;*.jpeg;*.webp)'];

/** xAI's status and what may be chosen. `null` in the simulation, which has no media core. */
export function useMediaStatus() {
  const { boot } = useServices();
  const { lang } = useI18n();
  return useResource<MediaStatus | null>((api) => (boot.mock ? Promise.resolve(null) : media.status(api, lang)), [boot.mock, lang]);
}

/** The previews of one kind, loaded again whenever an action of the screen finishes. */
export function useMediaItems(kind: MediaKind, enabled: boolean) {
  const area: MediaArea = kind === 'image' ? 'images' : 'video';
  const jobs = useAppState((s) => s.jobs);
  const finished = useMemo(
    () =>
      jobs
        .filter((j) => j.area === area && j.status === 'done')
        .map((j) => j.id)
        .join(' '),
    [jobs, area],
  );
  return useResource<MediaItem[]>((api) => (enabled ? media.items(api, kind) : Promise.resolve([])), [kind, enabled, finished]);
}

/** Shows a job that was just started at once, unless its own (newer) events already arrived. */
export function useAdoptJob() {
  const store = useStore();
  return useCallback(
    (job: Job | undefined) => {
      if (job && !store.getState().jobs.some((j) => j.id === job.id)) store.dispatch({ type: 'event', at: Date.now(), event: { type: 'job', data: job } });
    },
    [store],
  );
}

export function ConnectionChip({ status, kind }: { status: MediaStatus; kind: MediaKind }) {
  const { t } = useI18n();
  const ready = canCreate(status, kind);
  const local = isLocal(status);
  return (
    <Chip tone={ready ? 'ok' : 'warn'} dot testId="media-connection">
      {local ? t(ready ? 'media.localReady' : 'media.localMissing') : ready ? t('media.connected', { service: status.service }) : t('media.notConnected')}
    </Chip>
  );
}

/** What is missing for real creation, and how to check again after installing or connecting it. */
export function NotConnected({ status, kind, hint, testId, onCheck }: { status: MediaStatus; kind: MediaKind; hint: string; testId: string; onCheck: () => void }) {
  const { t } = useI18n();
  const local = isLocal(status);
  return (
    <Unavailable testId={testId} title={t(local ? 'media.localUnavailableTitle' : 'media.unavailableTitle')} reason={status.reason ?? ''} needs={missingFor(status, kind)}>
      <span className="field__hint">{local ? t('media.localUnavailableHint') : hint}</span>
      <div className="actions-row">
        <button type="button" className="btn btn--sm btn--ghost" onClick={onCheck} data-testid="media-check">
          <Icon name="restart" size={13} />
          {t('media.checkAgain')}
        </button>
      </div>
    </Unavailable>
  );
}

export interface Outgoing {
  name: string;
  /** A file of the user's (JARVIS asks before it is sent), not one of the screen's previews. */
  yours: boolean;
}

/** Roughly how long, in words ("about 40 seconds", "about 3 minutes"). */
export function useDuration() {
  const { t } = useI18n();
  return (seconds: number) => (seconds < 90 ? t('media.aboutSeconds', { n: Math.max(5, Math.round(seconds / 5) * 5) }) : t('media.aboutMinutes', { n: Math.round(seconds / 60) }));
}

/**
 * Shown above the button that starts the work. With xAI: exactly what leaves the computer, where to, and the
 * estimated price. With the models on this computer: that nothing leaves it, the text the model reads, that it is
 * free and roughly how long it takes (`seconds`).
 */
export function SendBox({ status, text, files, price, seconds = 0, notes, testId }: { status: MediaStatus; text: string; files: Outgoing[]; price: number; seconds?: number; notes?: string[]; testId: string }) {
  const { t } = useI18n();
  const duration = useDuration();
  if (isLocal(status)) {
    return (
      <div className="media-send media-send--local" data-testid={testId} data-engine="local">
        <span className="media-send__head">
          <Icon name="cpu" size={13} />
          {t('media.localHead')}
        </span>
        <span>{t('media.localReads')}</span>
        <pre className="mono-block media-send__text" data-testid={`${testId}-text`}>
          {text || '—'}
        </pre>
        {status.local?.translate && hasHebrew(text) ? (
          <span className="field__hint" data-testid={`${testId}-translate`}>
            {t('media.localTranslate')}
          </span>
        ) : null}
        {files.map((file, i) => (
          <span key={i} data-testid={`${testId}-file`}>
            <BidiText text={t(file.yours ? 'media.localYourFile' : 'media.localPreview', { name: file.name })} />
          </span>
        ))}
        <strong className="media-send__price" data-testid={`${testId}-price`}>
          {t('media.localPrice', { time: duration(seconds) })}
        </strong>
        <span className="field__hint">{t('media.localTimeNote')}</span>
        {notes?.map((note, i) => (
          <span key={i} className="field__hint">
            {note}
          </span>
        ))}
      </div>
    );
  }
  return (
    <div className="media-send" data-testid={testId}>
      <span className="media-send__head">
        <Icon name="send" size={13} />
        {t('media.sends')}
      </span>
      <span>{t('media.sendsTo', { service: status.service, host: status.host })}</span>
      <pre className="mono-block media-send__text" data-testid={`${testId}-text`}>
        {text || '—'}
      </pre>
      {files.map((file, i) => (
        <span key={i} data-testid={`${testId}-file`}>
          <BidiText text={t(file.yours ? 'media.sendsYourFile' : 'media.sendsPreview', { name: file.name })} />
        </span>
      ))}
      <strong className="media-send__price" data-testid={`${testId}-price`}>
        {t('media.price', { price: usd(price) })}
      </strong>
      <span className="field__hint">{t('media.priceAsOf', { date: status.offer.prices_checked })}</span>
      {notes?.map((note, i) => (
        <span key={i} className="field__hint">
          {note}
        </span>
      ))}
    </div>
  );
}

/** In place of the send box when only a demonstration can run: nothing leaves the computer. */
export function DemoBox({ text, testId }: { text: string; testId: string }) {
  const { t } = useI18n();
  return (
    <div className="media-send media-send--demo" data-testid={testId}>
      <span className="media-send__head">
        <Icon name="eye" size={13} />
        {t('job.demo')}
      </span>
      <span>{text}</span>
    </div>
  );
}

/** A real video plays; anything else is a picture. A demonstration says so on top of it, whatever it shows. */
export function MediaView({ item, controls }: { item: MediaItem; controls?: boolean }) {
  const { t } = useI18n();
  return (
    <>
      {item.kind === 'video' && item.real ? (
        <video src={item.url} controls={controls} muted={!controls} preload="metadata" playsInline data-testid="media-video" />
      ) : (
        <img src={item.url} alt={item.description} loading="lazy" data-testid="media-image" />
      )}
      {item.real ? null : (
        <span className="media-demo" data-testid="media-demo-badge">
          {t(item.kind === 'image' ? 'images.demoBadge' : 'video.demoBadge')}
        </span>
      )}
    </>
  );
}

/** Format, size, length and price of a preview, as short labelled facts. */
export function MediaFacts({ item }: { item: MediaItem }) {
  const { t } = useI18n();
  const local = item.params.engine === 'local';
  const price = local
    ? t('media.free')
    : item.usd !== null && item.usd !== undefined
      ? t('media.charged', { price: usd(item.usd) })
      : item.cost
        ? t('media.estimate', { price: item.cost })
        : null;
  const facts: [string, string][] = [[t('media.format'), item.format]];
  if (item.width && item.height) facts.push([t('media.pixels'), `${item.width}×${item.height}`]);
  if (item.seconds) facts.push([t('media.length'), t('media.secondsValue', { n: Math.round(item.seconds * 10) / 10 })]);
  if (item.bytes) facts.push([t('media.bytes'), formatBytes(item.bytes)]);
  return (
    <dl className="media-facts" data-testid="media-facts">
      {facts.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd className="num">{value}</dd>
        </div>
      ))}
      {price ? (
        <div>
          <dt>{t('job.cost')}</dt>
          <dd className="num" data-testid="media-price">
            {price}
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

export function Gallery({ kind, items, selected, onSelect }: { kind: MediaKind; items: MediaItem[]; selected: string | null; onSelect: (id: string) => void }) {
  const { t } = useI18n();
  if (!items.length) return <EmptyState icon={kind === 'image' ? 'image' : 'film'} title={t('media.empty')} hint={t(kind === 'image' ? 'images.emptyHint' : 'video.emptyHint')} testId="media-empty" />;
  return (
    <div className="media-grid" data-testid="media-grid">
      {items.map((item) => (
        <figure key={item.id} className="media-tile" data-testid="media-item" data-id={item.id} data-real={String(item.real)} data-selected={item.id === selected || undefined}>
          <button
            type="button"
            className={`media-tile__view ${item.kind === 'video' ? 'media-tile__view--video' : ''}`}
            onClick={() => onSelect(item.id)}
            aria-pressed={item.id === selected}
            aria-label={`${t('media.open')}: ${item.description || item.name}`}
            data-testid="media-item-open"
          >
            <MediaView item={item} />
          </button>
          <figcaption className="media-tile__meta">
            <span className="num">{item.format}</span>
            {item.width && item.height ? <span className="num">{`${item.width}×${item.height}`}</span> : null}
            {item.seconds ? <span className="num">{t('media.secondsValue', { n: Math.round(item.seconds) })}</span> : null}
            {item.usd !== null && item.usd !== undefined ? <span className="num">{usd(item.usd)}</span> : null}
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

/**
 * Save a real preview where the user picks, as its real format. A file that is already there is replaced only
 * after the user says so here (the core answers 409 "exists" first); the copy itself asks for approval as usual.
 */
export function SaveControl({ item }: { item: MediaItem }) {
  const { t, lang } = useI18n();
  const api = useApi();
  const toast = useToast();
  const adopt = useAdoptJob();
  const [target, setTarget] = useState<string | null>(null);
  const [conflict, setConflict] = useState<{ target: string; message: string } | null>(null);
  const [busy, setBusy] = useState(false);

  if (!item.real) {
    return (
      <button type="button" className="btn btn--sm" disabled title={t('media.sentNothing')} data-testid="media-save-disabled">
        <Icon name="save" size={13} />
        {t('media.saveDemo')}
      </button>
    );
  }
  const ext = EXTENSION[item.format] ?? item.format.toLowerCase();
  const save = async (path: string, overwrite: boolean) => {
    setBusy(true);
    try {
      adopt(await media.save(api, { item_id: item.id, target: path, overwrite, lang }));
      setConflict(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.code === 'exists') setConflict({ target: path, message: err.message });
      else toast('error', t('job.actionFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const last = item.saved_to.length ? item.saved_to[item.saved_to.length - 1] : null;
  return (
    <div className="media-save" data-testid="media-save">
      <PathPicker
        kind="save"
        value={target}
        onChange={(path) => {
          setTarget(path);
          setConflict(null);
          if (path) void save(path, false);
        }}
        label={t('media.saveAs', { format: item.format })}
        saveName={item.name}
        fileTypes={[`${item.format} (*.${ext})`]}
        testId="media-save-pick"
        disabled={busy}
      />
      <span className="field__hint">{t('media.saveHint', { format: item.format, ext })}</span>
      {conflict ? (
        <div className="media-conflict" role="alert" data-testid="media-save-conflict">
          <BidiText text={conflict.message} />
          <span className="actions-row">
            <button type="button" className="btn btn--sm btn--danger" disabled={busy} onClick={() => void save(conflict.target, true)} data-testid="media-save-replace">
              {t('media.replace')}
            </button>
            <button type="button" className="btn btn--sm btn--ghost" onClick={() => setConflict(null)} data-testid="media-save-keep">
              {t('media.keep')}
            </button>
          </span>
        </div>
      ) : null}
      {last ? (
        <span className="field__hint" data-testid="media-saved-to">
          <BidiText text={t('media.savedTo', { path: last })} />
        </span>
      ) : null}
    </div>
  );
}

export function DeleteButton({ item, onDeleted }: { item: MediaItem; onDeleted: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const toast = useToast();
  const { run, isPending } = useRunner(t('job.actionFailed'));
  return (
    <ConfirmButton
      confirmLabel={t('media.deleteConfirm')}
      disabled={isPending('delete')}
      onConfirm={() =>
        void run('delete', () => media.remove(api, item.id)).then((done) => {
          if (!done) return;
          toast('success', t('media.deleted'));
          onDeleted();
        })
      }
      testId="media-delete"
    >
      <Icon name="trash" size={13} />
      {t('media.delete')}
    </ConfirmButton>
  );
}

/** What a finished job left: its summary, and its previews (the ones still kept) to open. */
function jobResult(job: Job, known: ReadonlySet<string>, onSelect: (id: string) => void): ReactNode {
  if (job.status !== 'done' || !job.result) return null;
  const summary = typeof job.result.summary === 'string' ? job.result.summary : '';
  const items = jobItems(job).filter((item) => known.has(item.id));
  if (!summary && !items.length) return null;
  return (
    <div className="media-result" data-testid="media-result">
      {summary ? <BidiText text={summary} /> : null}
      {items.length ? (
        <div className="media-thumbs">
          {items.map((item) => (
            <button key={item.id} type="button" className="media-thumb" onClick={() => onSelect(item.id)} title={item.name} data-testid="media-thumb">
              <MediaView item={item} />
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** The screen's actions, newest first, each with its steps, Stop / Try again and what it made. */
export function MediaJobs({ area, known, onSelect }: { area: MediaArea; known: ReadonlySet<string>; onSelect: (id: string) => void }) {
  const { t } = useI18n();
  const jobs = useAppState((s) => s.jobs);
  const mine = useMemo(() => jobs.filter((j) => j.area === area), [jobs, area]);
  if (!mine.length) return null;
  return (
    <section className="area__stack" aria-label={t('media.history')} data-testid="media-jobs">
      <h2 className="media-heading">{t('media.history')}</h2>
      <div className="job-list">
        {mine.map((job) => (
          <JobCard key={job.id} job={job}>
            {jobResult(job, known, onSelect)}
          </JobCard>
        ))}
      </div>
    </section>
  );
}
