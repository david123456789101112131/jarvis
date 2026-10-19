import { useState } from 'react';
import { ApiError } from '../../api/client';
import { work, type DialogKind } from '../../api/work';
import { useI18n } from '../../i18n';
import { errorMessage, useApi, useToast } from '../../state/context';
import { BidiText } from '../BidiText';
import { Icon } from '../Icon';

/**
 * A file or folder reaches JARVIS only when the user picks it here: the window's own Open / Folder / Save dialog,
 * or (when JARVIS runs without its window) a path the user types and confirms.
 */
export function PathPicker({
  kind,
  value,
  onChange,
  label,
  fileTypes,
  saveName,
  directory,
  testId,
  disabled,
}: {
  kind: DialogKind;
  value: string | null;
  onChange: (path: string | null) => void;
  label?: string;
  fileTypes?: string[];
  saveName?: string;
  directory?: string;
  testId?: string;
  disabled?: boolean;
}) {
  const { t } = useI18n();
  const api = useApi();
  const toast = useToast();
  const [typing, setTyping] = useState(false);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const id = testId ?? `pick-${kind}`;
  const buttonLabel = label ?? t(kind === 'folder' ? 'pick.chooseFolder' : kind === 'save' ? 'pick.chooseSave' : 'pick.chooseFile');

  const open = async () => {
    setBusy(true);
    try {
      const paths = await work.dialog(api, { kind, file_types: fileTypes, save_name: saveName, directory: directory ?? (value || undefined) });
      if (paths && paths.length) onChange(paths[0]);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 409 || err.status === 501)) setTyping(true);
      else toast('error', t('job.actionFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="picker" data-testid={id}>
      <div className="picker__row">
        <button type="button" className="btn btn--sm" disabled={disabled || busy} onClick={() => void open()} data-testid={`${id}-open`}>
          <Icon name={kind === 'folder' ? 'folder' : kind === 'save' ? 'save' : 'file'} size={14} />
          {buttonLabel}
        </button>
        {value ? (
          <>
            <span className="picker__value" title={value} data-testid={`${id}-value`}>
              <span className="job__label">{t('pick.chosen')}: </span>
              <BidiText text={value} />
            </span>
            <button type="button" className="btn btn--sm btn--ghost" disabled={disabled} onClick={() => onChange(null)} data-testid={`${id}-clear`}>
              {t('pick.clear')}
            </button>
          </>
        ) : (
          <span className="picker__none">{t('pick.none')}</span>
        )}
      </div>
      {typing ? (
        <form
          className="picker__typed"
          onSubmit={(e) => {
            e.preventDefault();
            const path = typed.trim();
            if (!path) return;
            onChange(path);
            setTyping(false);
          }}
        >
          <label className="picker__typed-label" htmlFor={`${id}-typed`}>
            {t('pick.typed')}
          </label>
          <input id={`${id}-typed`} className="input ltr" dir="ltr" value={typed} placeholder={t('pick.typedPlaceholder')} onChange={(e) => setTyped(e.target.value)} data-testid={`${id}-typed`} />
          <button type="submit" className="btn btn--sm btn--primary" disabled={!typed.trim()} data-testid={`${id}-use`}>
            {t('pick.use')}
          </button>
        </form>
      ) : null}
    </div>
  );
}
