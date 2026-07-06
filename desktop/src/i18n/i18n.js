// i18next configuration for the desktop app.
//
// Locale JSON is bundled by Vite (no HTTP backend).  One namespace per page +
// a shared ``common`` namespace; ``fallbackLng: "en"`` keeps the app fully
// usable while translations are rolled out page by page.

import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import enCommon from "./locales/en/common.json";
import enLogin from "./locales/en/login.json";
import enAbout from "./locales/en/about.json";
import enLogs from "./locales/en/logs.json";
import enCredits from "./locales/en/credits.json";
import enDownloads from "./locales/en/downloads.json";
import enDashboard from "./locales/en/dashboard.json";
import enAvailableMachines from "./locales/en/availableMachines.json";
import enShared from "./locales/en/shared.json";
import enCreateRender from "./locales/en/createRender.json";
import enMyJobs from "./locales/en/myJobs.json";
import enConfiguration from "./locales/en/configuration.json";
import jaCommon from "./locales/ja/common.json";
import jaLogin from "./locales/ja/login.json";
import jaAbout from "./locales/ja/about.json";
import jaLogs from "./locales/ja/logs.json";
import jaCredits from "./locales/ja/credits.json";
import jaDownloads from "./locales/ja/downloads.json";
import jaDashboard from "./locales/ja/dashboard.json";
import jaAvailableMachines from "./locales/ja/availableMachines.json";
import jaShared from "./locales/ja/shared.json";
import jaCreateRender from "./locales/ja/createRender.json";
import jaMyJobs from "./locales/ja/myJobs.json";
import jaConfiguration from "./locales/ja/configuration.json";

const STORAGE_KEY = "pcrent_lang";
const SUPPORTED = ["en", "ja"];

const resources = {
  en: { common: enCommon, login: enLogin, about: enAbout, logs: enLogs, credits: enCredits, downloads: enDownloads, dashboard: enDashboard, availableMachines: enAvailableMachines, shared: enShared, createRender: enCreateRender, myJobs: enMyJobs, configuration: enConfiguration },
  ja: { common: jaCommon, login: jaLogin, about: jaAbout, logs: jaLogs, credits: jaCredits, downloads: jaDownloads, dashboard: jaDashboard, availableMachines: jaAvailableMachines, shared: jaShared, createRender: jaCreateRender, myJobs: jaMyJobs, configuration: jaConfiguration },
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
  ns: ["common", "login", "about", "logs", "credits", "downloads", "dashboard", "availableMachines", "shared", "createRender", "myJobs", "configuration"],
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
