import './web-adapter' // installs window.slidesApi/desktop/projectApi before App mounts
import React from 'react'
import { createRoot } from 'react-dom/client'
import { htmlLang, type Lang } from '@genoffice/i18n'
import { App } from './App'
import { AudienceView } from './components/AudienceView'
import { LocaleProvider, setModuleLang } from './i18n/locale'
import './styles.css'

// ?mode=audience: the presenter view's external-screen audience show window (created by the main process)
const mode = new URLSearchParams(window.location.search).get('mode')

// macOS windows are created with vibrancy; let the thumbnail pane show it
// (the audience show window stays fully opaque)
if (mode !== 'audience' && navigator.platform.toLowerCase().includes('mac'))
  document.body.classList.add('vib')

const embed = new URLSearchParams(location.search).get('embed') === '1'
if (embed) {
  document.body.classList.add('office-embed')
  const style = document.createElement('style')
  style.textContent = '.office-embed .ai-dock,.office-embed .ai-panel,.office-embed .ai-entry,.office-embed .ribbon-group:has(.ai-entry){display:none!important}'
    + '@media (max-width: 1000px){.office-embed .ribbon{display:none!important}}'
  document.head.appendChild(style)
}

async function bootstrap(): Promise<void> {
  let lang: Lang = 'zh'
  try {
    lang = await window.slidesApi.getLanguage()
  } catch {
    /* dev renderer without the preload handler */
  }
  setModuleLang(lang)
  document.documentElement.lang = htmlLang(lang)
  createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <LocaleProvider initial={lang}>
        {mode === 'audience' ? <AudienceView /> : <App />}
      </LocaleProvider>
    </React.StrictMode>,
  )
}

void bootstrap()
