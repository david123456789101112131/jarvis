// Strings of the "Video creation" area.

export const videoEn = {
  'video.title': 'Video creation',
  'video.subtitle': 'Describe a scene, preview the clip, refine it, save it where you choose.',
} as const;

export const videoHe: Record<keyof typeof videoEn, string> = {
  'video.title': 'יצירת וידאו',
  'video.subtitle': 'מתארים סצנה, רואים את הסרטון, מתקנים ושומרים איפה שבוחרים.',
};
