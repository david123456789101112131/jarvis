import { useMemo, useState } from 'react';
import {
  canCreate,
  changePrompt,
  imageCost,
  imagePrompt,
  isLocal,
  localChangePrompt,
  localSeconds,
  media,
  usd,
  variationPrompt,
  type MediaItem,
  type MediaStatus,
} from '../../api/media';
import { Icon } from '../../components/Icon';
import { ScreenHeader } from '../../components/shell/Shell';
import { ErrorBox, Loading, Panel, Segmented } from '../../components/ui';
import { RealBadge, Unavailable } from '../../components/work/JobCard';
import { PathPicker } from '../../components/work/PathPicker';
import { useI18n } from '../../i18n';
import { fileName } from '../../lib/format';
import { useApi, useRunner, useServices } from '../../state/context';
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
  useDuration,
  useMediaItems,
  useMediaStatus,
} from './MediaParts';

/**
 * The "Image creation" area: describe an image, see the text the model gets (and, with xAI, exactly what is sent and
 * the price; on this computer, that it is free and roughly how long), create it (or, when the models are not
 * installed, a demonstration that says so), then save, change or vary the preview.
 */
export function ImagesScreen() {
  const { t } = useI18n();
  const { boot } = useServices();
  const status = useMediaStatus();
  const s = status.data ?? null;
  const items = useMediaItems('image', s !== null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const list = useMemo(() => items.data ?? [], [items.data]);
  const known = useMemo(() => new Set(list.map((i) => i.id)), [list]);
  const selected = list.find((i) => i.id === selectedId) ?? null;

  return (
    <div className="area media" data-testid="images-screen">
      <ScreenHeader title={t('images.title')} subtitle={t('images.subtitle')} icon="image" actions={s ? <ConnectionChip status={s} kind="image" /> : null} />
      {boot.mock ? <Unavailable testId="images-unavailable" reason={t('unavailable.simulation')} /> : null}
      {status.error ? <ErrorBox message={status.error} onRetry={status.reload} retryLabel={t('media.checkAgain')} /> : null}
      {!boot.mock && !s && status.loading ? <Loading label={t('images.title')} /> : null}
      {s ? (
        <div className="area__cols">
          <div className="area__stack">
            {canCreate(s, 'image') ? null : <NotConnected status={s} kind="image" hint={t('images.unavailableHint')} testId="images-unavailable" onCheck={status.reload} />}
            <ImageForm status={s} />
          </div>
          <div className="area__stack">
            <Panel title={t('media.previews')} icon="image" testId="images-previews" meta={list.length ? <span className="num">{list.length}</span> : undefined}>
              {items.error ? <ErrorBox message={items.error} onRetry={items.reload} /> : <Gallery kind="image" items={list} selected={selectedId} onSelect={setSelectedId} />}
            </Panel>
            {selected ? (
              <ImageDetail
                key={selected.id}
                item={selected}
                status={s}
                onDeleted={() => {
                  setSelectedId(null);
                  items.reload();
                }}
              />
            ) : null}
            <MediaJobs area="images" known={known} onSelect={setSelectedId} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ImageForm({ status }: { status: MediaStatus }) {
  const { t, lang } = useI18n();
  const api = useApi();
  const adopt = useAdoptJob();
  const { run, isPending } = useRunner(t('job.actionFailed'));
  const duration = useDuration();
  const offer = status.offer;
  const first = offer.image.models[0];
  // A model may make only some sizes and shapes (the one on this computer makes about 1 megapixel, no "auto").
  const aspects = first?.aspect_ratios?.length ? first.aspect_ratios : offer.image.aspect_ratios;
  const resolutions = first?.resolutions?.length ? first.resolutions : offer.image.resolutions;
  const [description, setDescription] = useState('');
  const [styleId, setStyleId] = useState(offer.image.styles[0]?.id ?? 'none');
  const [model, setModel] = useState(first?.id ?? '');
  const [aspect, setAspect] = useState(aspects[0] ?? '1:1');
  const [resolution, setResolution] = useState(resolutions[0] ?? '1k');
  const [count, setCount] = useState(1);
  const [source, setSource] = useState<string | null>(null);
  const real = canCreate(status, 'image');
  const local = isLocal(status);
  const prompt = imagePrompt(offer, description, styleId);
  const tooLong = prompt.length > offer.max_prompt;
  const price = imageCost(offer, model, resolution, count, source ? 1 : 0);
  const cost = local ? t('media.free') : usd(price);
  const ready = description.trim().length > 0 && !tooLong && !isPending('create');

  const create = () =>
    void run('create', () =>
      media.createImage(api, { description, style: styleId, model, count, aspect_ratio: aspect, resolution, source_path: real ? source : null, demo: !real, lang }),
    ).then(adopt);

  return (
    <Panel title={t('images.form')} icon="wand" testId="images-form">
      {/* Not a <form>: the file picker inside has its own, and a paid request is only ever sent by its button. */}
      <div className="media-form">
        <label className="field">
          <span className="field__label">{t('images.description')}</span>
          <textarea
            className="textarea"
            dir="auto"
            value={description}
            maxLength={offer.max_prompt}
            placeholder={t('images.descriptionPlaceholder')}
            onChange={(e) => setDescription(e.target.value)}
            data-testid="images-description"
          />
          <span className="media-counter num" data-over={tooLong || undefined} data-testid="images-counter">
            {t('media.chars', { n: prompt.length, max: offer.max_prompt })}
          </span>
        </label>

        <div className="field-row">
          <label className="field">
            <span className="field__label">{t('images.style')}</span>
            <select className="select" value={styleId} onChange={(e) => setStyleId(e.target.value)} data-testid="images-style">
              {offer.image.styles.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field__label">{t('images.aspect')}</span>
            <select className="select" value={aspect} disabled={source !== null} onChange={(e) => setAspect(e.target.value)} data-testid="images-aspect">
              {aspects.map((a) => (
                <option key={a} value={a}>
                  {a === 'auto' ? t('images.aspectAuto') : a}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="field">
          <span className="field__label">{t('images.model')}</span>
          <Segmented
            value={model}
            options={offer.image.models.map((m) => ({ value: m.id, label: m.label, title: local ? undefined : t('images.perImage', { price: usd(imageCost(offer, m.id, resolution, 1)) }) }))}
            onChange={setModel}
            label={t('images.model')}
            testId="images-model"
            fit
          />
          <span className="field__hint num" data-testid="images-model-hint">
            {local ? t('images.perImageLocal', { time: duration(offer.local_times?.image_s ?? 0) }) : t('images.perImage', { price: usd(imageCost(offer, model, resolution, 1)) })}
          </span>
        </div>

        <div className="field-row">
          <div className="field">
            <span className="field__label">{t('images.resolution')}</span>
            {/* An image being changed keeps its own shape and size: neither is sent with it. */}
            <Segmented value={resolution} options={resolutions.map((r) => ({ value: r, label: r.toUpperCase() }))} onChange={setResolution} label={t('images.resolution')} testId="images-resolution" disabled={source !== null} />
          </div>
          <div className="field">
            <span className="field__label">{t('images.count')}</span>
            <Segmented
              value={String(count)}
              options={Array.from({ length: offer.image.max_images }, (_, i) => ({ value: String(i + 1), label: String(i + 1) }))}
              onChange={(v) => setCount(Number(v))}
              label={t('images.count')}
              testId="images-count"
            />
          </div>
        </div>

        {real ? (
          <div className="field">
            <span className="field__label">{t('images.source')}</span>
            <PathPicker kind="open" value={source} onChange={setSource} fileTypes={IMAGE_FILE_TYPES} testId="images-source" />
            <span className="field__hint">{t(local ? 'images.sourceHintLocal' : 'images.sourceHint')}</span>
          </div>
        ) : null}

        {real ? (
          <SendBox
            status={status}
            text={prompt}
            files={source ? [{ name: fileName(source), yours: true }] : []}
            price={price}
            seconds={localSeconds(status, 'image', { count })}
            notes={[t('media.keptHere')]}
            testId="images-sends"
          />
        ) : (
          <DemoBox text={t('images.demoNote', { label: t('images.demoBadge') })} testId="images-sends" />
        )}

        <div className="actions-row">
          {real ? (
            <button type="button" className="btn btn--primary" disabled={!ready} onClick={create} data-testid="images-create">
              <Icon name="sparkles" size={14} />
              {count > 1 ? t('images.createMany', { n: count, price: cost }) : t('images.create', { price: cost })}
            </button>
          ) : (
            <button type="button" className="btn" disabled={!ready} onClick={create} data-testid="images-demo">
              <Icon name="eye" size={14} />
              {t('images.demo')}
            </button>
          )}
        </div>
      </div>
    </Panel>
  );
}

type RefineMode = 'change' | 'variation';

/** The chosen preview, large: what was sent for it, Save, Change or a variation, and Delete. */
function ImageDetail({ item, status, onDeleted }: { item: MediaItem; status: MediaStatus; onDeleted: () => void }) {
  const { t, lang } = useI18n();
  const api = useApi();
  const adopt = useAdoptJob();
  const { run, isPending } = useRunner(t('job.actionFailed'));
  const offer = status.offer;
  const [mode, setMode] = useState<RefineMode>('change');
  const [change, setChange] = useState('');
  const base = item.description || item.prompt;
  const variation = mode === 'variation';
  // On this computer a change redraws the preview from text, so the text says what the image should become.
  const localModel = item.params.engine === 'local' || (offer.image.models.find((m) => m.id === String(item.params.model ?? ''))?.local ?? false);
  const text = localModel ? (variation ? base : localChangePrompt(base, change)) : variation ? variationPrompt(base) : changePrompt(base, change);
  const price = imageCost(offer, String(item.params.model ?? ''), String(item.params.resolution ?? ''), 1, 1);
  const cost = isLocal(status) ? t('media.free') : usd(price);
  const blocked = item.real && !canCreate(status, 'image');
  const ready = !blocked && (variation || change.trim().length > 0) && text.length <= offer.max_prompt && !isPending('refine');

  const refine = () =>
    void run('refine', () => media.refineImage(api, { item_id: item.id, change: variation ? '' : change, variation, count: 1, lang })).then((job) => {
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
          <span className="field__label">{t(item.params.engine === 'local' ? 'media.modelRead' : 'media.sent')}</span>
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
          <Segmented
            value={mode}
            options={[
              { value: 'change', label: t('images.mode.change') },
              { value: 'variation', label: t('images.mode.variation') },
            ]}
            onChange={setMode}
            label={t('images.mode')}
            testId="images-refine-mode"
          />
          {variation ? null : (
            <label className="field">
              <span className="field__label">{t('images.change')}</span>
              <textarea className="textarea media-refine__text" dir="auto" value={change} maxLength={offer.max_prompt} placeholder={t('images.changePlaceholder')} onChange={(e) => setChange(e.target.value)} data-testid="images-change" />
            </label>
          )}
          {blocked ? (
            <span className="field__hint" data-testid="images-refine-blocked">
              {t(isLocal(status) ? 'images.needsLocal' : 'images.needsXai')}
            </span>
          ) : item.real ? (
            <SendBox status={status} text={text} files={[{ name: item.name, yours: false }]} price={price} seconds={localSeconds(status, 'image')} testId="images-refine-sends" />
          ) : (
            <DemoBox text={t('images.refineDemoNote')} testId="images-refine-sends" />
          )}
          <div className="actions-row">
            <button type="button" className={`btn ${item.real ? 'btn--primary' : ''}`} disabled={!ready} onClick={refine} data-testid="images-refine">
              <Icon name={variation ? 'layers' : 'wand'} size={14} />
              {item.real
                ? t(variation ? 'images.variationRun' : 'images.changeRun', { price: cost })
                : t(variation ? 'images.variationDemo' : 'images.changeDemo')}
            </button>
            <DeleteButton item={item} onDeleted={onDeleted} />
          </div>
        </div>
      </div>
    </Panel>
  );
}
