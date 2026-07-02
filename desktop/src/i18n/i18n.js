// i18next configuration for the desktop app.
//
// Locale JSON is bundled by Vite (no HTTP backend).  One namespace per page +
// a shared ``common`` namespace; ``fallbackLng: "en"`` keeps the app fully
// usable while translations are rolled out page by page.

import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import enCommon from "./locales/en/common.json";
import enLogin from "./locales/en/login.json";
import jaCommon from "./locales/ja/common.json";
import jaLogin from "./locales/ja/login.json";

const STORAGE_KEY = "pcrent_lang";
const SUPPORTED = ["en", "ja"];

const resources = {
  en: { common: enCommon, login: enLogin },
  ja: { common: jaCommon, login: jaLogin },
};

function initialLanguage() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && SUPPORTED.includes(saved)) return saved;
  } catch {
    // localStorage unavailable -- fall through to browser default.
  }
  const nav = (navigator.language || "en").toLowerCase();
  return nav.startsWith("ja") ? "ja" : "en";
}

i18n.use(initReactI18next).init({
  resources,
  lng: initialLanguage(),
  fallbackLng: "en",
  supportedLngs: SUPPORTED,
  ns: ["common", "login"],
  defaultNS: "common",
  returnEmptyString: false,
  interpolation: { escapeValue: false }, // React already escapes.
});

// Switch language and persist the choice for next launch.
export function setLanguage(lng) {
  if (!SUPPORTED.includes(lng)) return;
  i18n.changeLanguage(lng);
  try {
    localStorage.setItem(STORAGE_KEY, lng);
  } catch {
    // Persistence is best-effort; the in-memory switch still applies.
  }
}

export const SUPPORTED_LANGUAGES = SUPPORTED;
export default i18n;
