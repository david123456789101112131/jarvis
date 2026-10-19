import { ScreenHeader } from '../../components/shell/Shell';
import { useI18n } from '../../i18n';

/** What JARVIS keeps on this computer, and clearing it. */
export function PrivacyScreen() {
  const { t } = useI18n();
  return (
    <div className="area" data-testid="privacy-screen">
      <ScreenHeader title={t('privacy.title')} subtitle={t('privacy.subtitle')} icon="lock" />
    </div>
  );
}
