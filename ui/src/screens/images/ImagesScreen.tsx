import { useMemo, useState } from 'react';
import { changePrompt, imageCost, imagePrompt, media, usd, variationPrompt, type MediaItem, type MediaStatus } from '../../api/media';
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
  useMediaItems,
  useMediaStatus,
} from './MediaParts';

/**
 * The "Image creation" area: describe an image, see exactly what is sent to xAI and the price, create it (or, when
 * xAI is not connected, a demonstration that says so), then save, change or vary the preview.
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
      <ScreenHeader title={t('images.title')} subtitle={t('images.subtitle')} icon="image" actions={s ? <ConnectionChip status={s} /> : null} />
      {boot.mock ? <Unavailable testId="images-unavailable" reason={t('unavailable.simulation')} /> : null}
      {status.error ? <ErrorBox message={status.error} onRetry={status.reload} retryLabel={t('media.checkAgain')} /> : null}
      {!boot.mock && !s && status.loading ? <Loading label={t('images.title')} /> : null}
      {s ? (
        <div className="area__cols">
          <div className="area__stack">
            {s.available ? null : <NotConnected status={s} hint={t('images.unavailableHint')} testId="images-unavailable" onCheck={status.reload} />}
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
  const offer = status.offer;
  const [description, setDescription] = useState('');
  const [styleId, setStyleId] = useState(offer.image.styles[0]?.id ?? 'none');
  const [model, setModel] = useState(offer.image.models[0]?.id ?? '');
  const [aspect, setAspect] = useState(offer.image.aspect_ratios[0] ?? '1:1');
  const [resolution, setResolution] = useState(offer.image.resolutions[0] ?? '1k');
  const [count, setCount] = useState(1);
  const [source, setSource] = useState<string | null>(null);
  const real = status.available;
  const prompt = imagePrompt(offer, description, styleId);
  const tooLong = prompt.length > offer.max_prompt;
  const price = imageCost(offer, model, resolution, count, source ? 1 : 0);
  const ready = description.trim().length > 0 && !tooLong && !isPending('create');

  const create = () =>
    void run('create', () =>
      media.createImage(api, { description, style: styleId, model, count, aspect_ratio: aspect, resolution, source_path: real ? source : null, demo: !real, lang }),
    ).then(adopt);

  return (
    <Panel title={t('images.form')} icon="wand" testId="images-form">
      <form
        className="media-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready) create();
        }}
      >
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
            <select className="select ltr" dir="ltr" value={aspect} disabled={source !== null} onChange={(e) => setAspect(e.target.value)} data-testid="images-aspect">
              {offer.image.aspect_ratios.map((a) => (
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
            options={offer.image.models.map((m) => ({ value: m.id, label: m.label, title: t('images.perImage', { price: usd(imageCost(offer, m.id, resolution, 1)) }) }))}
            onChange={setModel}
            label={t('images.model')}
            testId="images-model"
            fit
          />
          <span className="field__hint num">{t('images.perImage', { price: usd(imageCost(offer, model, resolution, 1)) })}</span>
        </div>

        <div className="field-row">
          <div className="field">
            <span className="field__label">{t('images.resolution')}</span>
            {/* An image being changed keeps its own shape and size: neither is sent with it. */}
            <Segmented value={resolution} options={offer.image.resolutions.map((r) => ({ value: r, label: r.toUpperCase() }))} onChange={setResolution} label={t('images.resolution')} testId="images-resolution" disabled={source !== null} />
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
            <span className="field__hint">{t('images.sourceHint')}</span>
          </div>
        ) : null}

        {real ? (
          <SendBox status={status} text={prompt} files={source ? [{ name: fileName(source), yours: true }] : []} price={price} notes={[t('media.keptHere')]} testId="images-sends" />
        ) : (
          <DemoBox text={t('images.demoNote', { label: t('images.demoBadge') })} testId="images-sends" />
        )}

        <div className="actions-row">
          {real ? (
            <button type="submit" className="btn btn--primary" disabled={!ready} data-testid="images-create">
              <Icon name="sparkles" size={14} />
              {count > 1 ? t('images.createMany', { n: count, price: usd(price) }) : t('images.create', { price: usd(price) })}
            </button>
          ) : (
            <button type="submit" className="btn" disabled={!ready} data-testid="images-demo">
              <Icon name="eye" size={14} />
              {t('images.demo')}
            </button>
          )}
        </div>
      </form>
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
  const text = variation ? variationPrompt(base) : changePrompt(base, change);
  const price = imageCost(offer, String(item.params.model ?? ''), String(item.params.resolution ?? ''), 1, 1);
  const blocked = item.real && !status.available;
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
          <span className="field__label">{t('media.sent')}</span>
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
              {t('images.needsXai')}
            </span>
          ) : item.real ? (
            <SendBox status={status} text={text} files={[{ name: item.name, yours: false }]} price={price} testId="images-refine-sends" />
          ) : (
            <DemoBox text={t('images.refineDemoNote')} testId="images-refine-sends" />
          )}
          <div className="actions-row">
            <button type="button" className={`btn ${item.real ? 'btn--primary' : ''}`} disabled={!ready} onClick={refine} data-testid="images-refine">
              <Icon name={variation ? 'layers' : 'wand'} size={14} />
              {item.real
                ? t(variation ? 'images.variationRun' : 'images.changeRun', { price: usd(price) })
                : t(variation ? 'images.variationDemo' : 'images.changeDemo')}
            </button>
            <DeleteButton item={item} onDeleted={onDeleted} />
          </div>
        </div>
      </div>
    </Panel>
  );
}
