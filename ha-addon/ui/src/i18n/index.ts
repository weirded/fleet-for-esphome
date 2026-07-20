// I18N.1: i18next singleton.
//
// The actual locale catalogs live alongside in `locales/{en,de}.json`
// and grow over the I18N workstream (#141). At I18N.1 they are empty
// objects — the foundation is in place but the UI still renders the
// English literals it always has, because no key has been migrated yet.
// React-Trans / t() calls land progressively in I18N.4.
//
// fallbackLng: 'en' so a missing German key falls back to the English
// catalog rather than the raw key. escapeValue: false because React
// already escapes interpolated values — letting i18next escape on top
// double-encodes (`&amp;amp;` and friends).
//
// Language selection is driven from `AppSettings.language` in App.tsx
// via `i18n.changeLanguage()`; the 'auto' value there resolves to
// `navigator.language` before the call.

import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

import en from './locales/en.json';
import de from './locales/de.json';

void i18n
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      de: { translation: de },
    },
    lng: 'en',
    fallbackLng: 'en',
    interpolation: {
      escapeValue: false,
    },
    returnNull: false,
  });

export default i18n;
