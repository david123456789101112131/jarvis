import { useMemo, useState } from 'react';
import { canCreate, isLocal, localSeconds, media, usd, videoChangePrompt, videoCost, videoPrompt, type MediaItem, type MediaStatus, type VideoPart } from '../../api/media';
import { Icon } from '../../components/Icon';
import { ScreenHeader } from '../../components/shell/Shell';
import { ErrorBox, Loading, Panel, Segmented, Slider } from '../../components/ui';
import { RealBadge, Unavailable } from '../../components/work/JobCard';
import { PathPicker } from '../../components/work/PathPicker';
import { useI18n, type MessageKey } from '../../i18n';
import { fileName } from '../../lib/format';
import { useApi, useResource, useRunner, useServices } from '../../state/context';
import {
  ConnectionChip,
  DeleteButton,
  DemoBox,
  Gallery,
  IMAGE_FILE_TYPES,
  MediaFacts,
  MediaJobs,
  MediaView,
  NotConnected,
  SaveControl,
  SendBox,
  useAdoptJob,
  useMediaItems,
  useMediaStatus,
  type Outgoing,
} from '../images/MediaParts';

/**
 * The "Video creation" area: describe the scene part by part, see the text the model gets (with xAI, exactly what is
 * sent and the price; on this computer, that it is free and roughly how long), create the clip and follow it until it
 * is ready, then save or change it. When the models are not installed (or xAI is not connected) there is no Create
 * button at all, only a demonstration of the steps that says so.
 */
export function VideoScreen() {
  const { t } = useI18n();
  const { boot } = useServices();
  const status = useMediaStatus();
  const s = status.data ?? null;
  const items = useMediaItems('video', s !== null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const list = useMemo(() => items.data ?? [], [items.data]);
  const known = useMemo(() => new Set(list.map((i) => i.id)), [list]);
  const selected = list.find((i) => i.id === selectedId) ?? null;

  return (
    <div className="area media" data-testid="video-screen">
      <ScreenHeader title={t('video.title')} subtitle={t('video.subtitle')} icon="film" actions={s ? <ConnectionChip status={s} kind="video" /> : null} />
      {boot.mock ? <Unavailable testId="video-unavailable" reason={t('unavailable.simulation')} /> : null}
      {status.error ? <ErrorBox message={status.error} onRetry={status.reload} retryLabel={t('media.checkAgain')} /> : null}
      {!boot.mock && !s && status.loading ? <Loading label={t('video.title')} /> : null}
      {s ? (
        <div className="area__cols">
          <div className="area__stack">
            {canCreate(s, 'video') ? null : <NotConnected status={s} kind="video" hint={t('video.unavailableHint')} testId="video-unavailable" onCheck={status.reload} />}
            <VideoForm status={s} />
          </div>
          <div className="area__stack">
            <Panel title={t('media.previews')} icon="film" testId="video-previews" meta={list.length ? <span className="num">{list.length}</span> : undefined}>
              {items.error ? <ErrorBox message={items.error} onRetry={items.reload} /> : <Gallery kind="video" items={list} selected={selectedId} onSelect={setSelectedId} />}
            </Panel>
            {selected ? (
              <VideoDetail
                key={selected.id}
                item={selected}
                status={s}
                onDeleted={() => {
                  setSelectedId(null);
                  items.reload();
                }}
              />
            ) : null}
            <MediaJobs area="video" known={known} onSelect={setSelectedId} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

type Start = 'none' | 'image' | 'references';

const EMPTY_PARTS: Record<VideoPart, string> = { scene: '', characters: '', motion: '', lighting: '', style: '' };

function VideoForm({ status }: { status: MediaStatus }) {
  const { t, lang } = useI18n();
  const api = useApi();
  const adopt = useAdoptJob();
  const { run, isPending } = useRunner(t('job.actionFailed'));
  const offer = status.offer;
  const real = canCreate(status, 'video');
  const local = isLocal(status);
  const [parts, setParts] = useState(EMPTY_PARTS);
  const [model, setModel] = useState(offer.video.models[0]?.id ?? '');
  const [seconds, setSeconds] = useState(offer.video.models[0]?.seconds?.default ?? offer.video.seconds.default);
  const [aspect, setAspect] = useState(offer.video.aspect_ratios[0] ?? '16:9');
  const [resolution, setResolution] = useState(offer.video.resolutions[0] ?? '480p');
  const [start, setStart] = useState<Start>('none');
  const [imagePath, setImagePath] = useState<string | null>(null);
  const [imageItem, setImageItem] = useState('');
  const [references, setReferences] = useState<string[]>([]);
  const [note, setNote] = useState<string | null>(null);
  // Real images made on the image screen can be a first frame (a demonstration placeholder cannot be sent).
  const pictures = useResource<MediaItem[]>((a) => (real ? media.items(a, 'image') : Promise.resolve([])), [real]);
  const previews = useMemo(() => (pictures.data ?? []).filter((i) => i.real), [pictures.data]);

  const info = offer.video.models.find((m) => m.id === model) ?? offer.video.models[0];
  const range = info?.seconds ?? offer.video.seconds;
  const resolutions = start === 'references' ? offer.video.reference_resolutions : (info?.resolutions ?? offer.video.resolutions);
  // Reference images only where a model takes them (the one on this computer starts from a first frame at most).
  const starts: Start[] = offer.video.models.some((m) => m.references) ? ['none', 'image', 'references'] : ['none', 'image'];
  const preview = previews.find((i) => i.id === imageItem) ?? null;
  const files: Outgoing[] =
    start === 'image'
      ? preview
        ? [{ name: preview.name, yours: false }]
        : imagePath
          ? [{ name: fileName(imagePath), yours: true }]
          : []
      : start === 'references'
        ? references.map((p) => ({ name: fileName(p), yours: true }))
        : [];
  const text = videoPrompt(offer, parts);
  const tooLong = text.length > offer.max_prompt;
  const price = videoCost(offer, model, resolution, seconds, files.length);
  const cost = local ? t('media.free') : usd(price);
  const ready = parts.scene.trim().length > 0 && !tooLong && !isPending('create');
  const firstFrame = start === 'image' && files.length > 0;

  const chooseStart = (next: Start) => {
    setStart(next);
    setNote(null);
    if (next !== 'references') return;
    if (!info?.references) {
      const full = offer.video.models.find((m) => m.references);
      if (full) {
        setModel(full.id);
        setNote(t('video.fullChosen'));
      }
    }
    if (!offer.video.reference_resolutions.includes(resolution)) setResolution(offer.video.reference_resolutions[offer.video.reference_resolutions.length - 1] ?? '480p');
  };
  const chooseModel = (id: string) => {
    setModel(id);
    setNote(null);
    const limits = offer.video.models.find((m) => m.id === id)?.seconds ?? offer.video.seconds;
    setSeconds((n) => Math.min(Math.max(n, limits.min), limits.max));
    if (start === 'references' && !offer.video.models.find((m) => m.id === id)?.references) setStart('none');
  };

  const create = () =>
    void run('create', () =>
      media.createVideo(api, {
        ...parts,
        model,
        seconds,
        aspect_ratio: aspect,
        resolution,
        image_item: real && start === 'image' && preview ? preview.id : null,
        image_path: real && start === 'image' && !preview ? imagePath : null,
        reference_items: [],
        reference_paths: real && start === 'references' ? references : [],
        demo: !real,
        lang,
      }),
    ).then(adopt);

  return (
    <Panel title={t('video.form')} icon="film" testId="video-form">
      {/* Not a <form>: the file pickers inside have their own, and a paid request is only ever sent by its button. */}
      <div className="media-form">
        <div className="video-parts">
          {offer.video.parts.map((key) => (
            <label key={key} className={`field ${key === 'scene' ? '' : 'video-part'}`}>
              <span className="field__label">{t(`video.part.${key}` as MessageKey)}</span>
              {key === 'scene' ? (
                <textarea
                  className="textarea"
                  dir="auto"
                  value={parts.scene}
                  maxLength={offer.max_prompt}
                  placeholder={t('video.placeholder.scene')}
                  onChange={(e) => setParts({ ...parts, scene: e.target.value })}
                  data-testid="video-part-scene"
                />
              ) : (
                <input
                  className="input"
                  dir="auto"
                  value={parts[key]}
                  maxLength={1000}
                  placeholder={t(`video.placeholder.${key}` as MessageKey)}
                  onChange={(e) => setParts({ ...parts, [key]: e.target.value })}
                  data-testid={`video-part-${key}`}
                />
              )}
            </label>
          ))}
          <span className="field__hint">{t('video.partsHint')}</span>
          <span className="media-counter num" data-over={tooLong || undefined}>
            {t('media.chars', { n: text.length, max: offer.max_prompt })}
          </span>
        </div>

        <div className="field">
          <span className="field__label">{t('video.model')}</span>
          <Segmented
            value={model}
            options={offer.video.models.map((m) => ({ value: m.id, label: m.label, title: local ? undefined : t('video.perSecond', { price: usd(videoCost(offer, m.id, resolution, 1)) }) }))}
            onChange={chooseModel}
            label={t('video.model')}
            testId="video-model"
            fit
          />
          <span className="field__hint num" data-testid="video-model-hint">
            {local ? t('video.localHint') : t('video.perSecond', { price: usd(videoCost(offer, model, resolution, 1)) })}
          </span>
        </div>

        <div className="field">
          <span className="field__label">{t('video.seconds')}</span>
          <div className="video-seconds">
            <Slider
              value={seconds}
              min={range.min}
              max={range.max}
              step={1}
              onChange={setSeconds}
              label={t('video.seconds')}
              format={(n) => t('video.secondsValue', { n })}
              testId="video-seconds"
            />
            <span className="video-seconds__value num" data-testid="video-seconds-value">
              {t('video.secondsValue', { n: seconds })}
            </span>
          </div>
        </div>

        <div className="field-row">
          <label className="field">
            <span className="field__label">{t('video.aspect')}</span>
            <select className="select" value={aspect} disabled={firstFrame} onChange={(e) => setAspect(e.target.value)} data-testid="video-aspect">
              {offer.video.aspect_ratios.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </label>
          <div className="field">
            <span className="field__label">{t('video.resolution')}</span>
            <Segmented value={resolution} options={resolutions.map((r) => ({ value: r, label: r }))} onChange={setResolution} label={t('video.resolution')} testId="video-resolution" />
          </div>
        </div>

        {real ? (
          <div className="field">
            <span className="field__label">{t('video.start')}</span>
            <Segmented
              value={start}
              options={starts.map((v) => ({ value: v, label: t(`video.start.${v}` as MessageKey) }))}
              onChange={chooseStart}
              label={t('video.start')}
              testId="video-start"
            />
            {note ? (
              <span className="video-note" data-testid="video-note">
                {note}
              </span>
            ) : null}
            {start === 'image' ? (
              <div className="field">
                <span className="field__label">{t('video.image')}</span>
                <PathPicker
                  kind="open"
                  value={imagePath}
                  onChange={(path) => {
                    setImagePath(path);
                    if (path) setImageItem('');
                  }}
                  fileTypes={IMAGE_FILE_TYPES}
                  testId="video-image"
                />
                {previews.length ? (
                  <label className="field">
                    <span className="field__label">{t('video.imagePreview')}</span>
                    <select
                      className="select"
                      value={imageItem}
                      onChange={(e) => {
                        setImageItem(e.target.value);
                        if (e.target.value) setImagePath(null);
                      }}
                      data-testid="video-image-preview"
                    >
                      <option value="">{t('video.imagePreviewNone')}</option>
                      {previews.map((i) => (
                        <option key={i.id} value={i.id}>
                          {i.description || i.name}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : null}
                <span className="field__hint">{t('video.imageHint')}</span>
              </div>
            ) : null}
            {start === 'references' ? (
              <div className="field">
                <span className="field__label">{t('video.references', { n: offer.video.max_references })}</span>
                {references.length ? (
                  <ul className="video-refs" data-testid="video-references">
                    {references.map((path, i) => (
                      <li key={`${path}-${i}`}>
                        <span className="ltr" dir="ltr" title={path}>
                          {fileName(path)}
                        </span>
                        <button type="button" className="btn btn--sm btn--ghost" onClick={() => setReferences(references.filter((_, j) => j !== i))} data-testid="video-reference-remove">
                          {t('video.removeReference')}
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {references.length < offer.video.max_references ? (
                  <PathPicker
                    kind="open"
                    value={null}
                    onChange={(path) => {
                      if (path && !references.includes(path)) setReferences([...references, path]);
                    }}
                    label={t('video.addReference')}
                    fileTypes={IMAGE_FILE_TYPES}
                    testId="video-add-reference"
                  />
                ) : null}
                <span className="field__hint">{t('video.referencesHint')}</span>
              </div>
            ) : null}
          </div>
        ) : null}

        {real ? (
          <SendBox
            status={status}
            text={text}
            files={files}
            price={price}
            seconds={localSeconds(status, 'video', { seconds, resolution })}
            notes={local ? [t('video.stopNoteLocal'), t('media.keptHere')] : [t('video.stopNote'), t('video.waitNote', { n: offer.video.wait_minutes }), t('media.keptHere')]}
            testId="video-sends"
          />
        ) : (
          <DemoBox text={t('video.demoNote', { label: t('video.demoBadge') })} testId="video-sends" />
        )}

        <div className="actions-row">
          {real ? (
            <button type="button" className="btn btn--primary" disabled={!ready} onClick={create} data-testid="video-create">
              <Icon name="sparkles" size={14} />
              {t('video.create', { price: cost })}
            </button>
          ) : (
            <button type="button" className="btn" disabled={!ready} onClick={create} data-testid="video-demo">
              <Icon name="eye" size={14} />
              {t('video.demo')}
            </button>
          )}
        </div>
      </div>
    </Panel>
  );
}

/** The chosen clip: it plays (or the storyboard shows), what was sent for it, Save, Change and Delete. */
function VideoDetail({ item, status, onDeleted }: { item: MediaItem; status: MediaStatus; onDeleted: () => void }) {
  const { t, lang } = useI18n();
  const api = useApi();
  const adopt = useAdoptJob();
  const { run, isPending } = useRunner(t('job.actionFailed'));
  const offer = status.offer;
  const [change, setChange] = useState('');
  const p = item.params;
  const along = (typeof p.image === 'string' && p.image ? 1 : 0) + (Array.isArray(p.references) ? p.references.length : 0);
  const text = videoChangePrompt(item.prompt, change);
  const price = videoCost(offer, String(p.model ?? ''), String(p.resolution ?? ''), Number(p.seconds ?? offer.video.seconds.default), along);
  const local = isLocal(status);
  const cost = local ? t('media.free') : usd(price);
  const blocked = item.real && !canCreate(status, 'video');
  const ready = !blocked && change.trim().length > 0 && text.length <= offer.max_prompt && !isPending('refine');

  const refine = () =>
    void run('refine', () => media.refineVideo(api, { item_id: item.id, change, lang })).then((job) => {
      adopt(job);
      if (job) setChange('');
    });

  return (
    <Panel title={t('media.selected')} icon="eye" testId="media-selected" meta={<RealBadge real={item.real} testId="media-selected-real" />}>
      <div className="media-detail">
        <div className="media-view">
          <MediaView item={item} controls />
        </div>
        <MediaFacts item={item} />
        <div className="field">
          <span className="field__label">{t(p.engine === 'local' ? 'media.modelRead' : 'media.sent')}</span>
          {item.real ? (
            <pre className="mono-block media-send__text" data-testid="media-sent">
              {item.prompt}
            </pre>
          ) : (
            <span className="field__hint" data-testid="media-sent">
              {t('media.sentNothing')}
            </span>
          )}
        </div>
        <SaveControl item={item} />

        <div className="media-refine">
          <label className="field">
            <span className="field__label">{t('video.change')}</span>
            <textarea className="textarea media-refine__text" dir="auto" value={change} maxLength={offer.max_prompt} placeholder={t('video.changePlaceholder')} onChange={(e) => setChange(e.target.value)} data-testid="video-change" />
          </label>
          {blocked ? (
            <span className="field__hint" data-testid="video-refine-blocked">
              {t(local ? 'video.needsLocal' : 'video.needsXai')}
            </span>
          ) : item.real ? (
            <SendBox
              status={status}
              text={text}
              files={[]}
              price={price}
              seconds={localSeconds(status, 'video', { seconds: Number(p.seconds ?? 0) || item.seconds || 0, resolution: String(p.resolution ?? '480p') })}
              notes={[...(along && !local ? [t('video.changeFiles')] : []), t(local ? 'video.stopNoteLocal' : 'video.stopNote')]}
              testId="video-refine-sends"
            />
          ) : (
            <DemoBox text={t('video.refineDemoNote')} testId="video-refine-sends" />
          )}
          <div className="actions-row">
            <button type="button" className={`btn ${item.real ? 'btn--primary' : ''}`} disabled={!ready} onClick={refine} data-testid="video-refine">
              <Icon name="wand" size={14} />
              {item.real ? t('video.changeRun', { price: cost }) : t('video.changeDemo')}
            </button>
            <DeleteButton item={item} onDeleted={onDeleted} />
          </div>
        </div>
      </div>
    </Panel>
  );
}
