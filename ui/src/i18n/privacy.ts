// Strings of the "Privacy & data" area.

export const privacyEn = {
  'privacy.title': 'Privacy & data',
  'privacy.subtitle': 'What JARVIS keeps on this computer, and clearing it.',
} as const;

export const privacyHe: Record<keyof typeof privacyEn, string> = {
  'privacy.title': 'פרטיות ונתונים',
  'privacy.subtitle': 'מה JARVIS שומר במחשב הזה, ואיך מוחקים את זה.',
};
