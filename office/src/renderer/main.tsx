import './web-adapter' // installs window.desktop / window.projectApi (must run first)
import { createRoot } from 'react-dom/client'
import { htmlLang, type Lang } from '@genoffice/i18n'
import { App } from './App'
import { Shell } from './shell/Shell'
import { LocaleProvider, setModuleLang } from './i18n/locale'
import './styles.css'
import './fonts/fonts.css'

async function bootstrap(): Promise<void> {
  let lang: Lang = 'zh'
  try {
    lang = await window.desktop.getLanguage()
  } catch {
    /* dev renderer without the preload handler */
  }
  setModuleLang(lang)
  document.documentElement.lang = htmlLang(lang)
  const embed = new URLSearchParams(location.search).get('embed') === '1'
  if (embed) {
    // Embedded in the workbench artifact pane: skip AIOffice's login/home shell.
    localStorage.setItem('aidocs.showAi', '0')
    document.body.classList.add('office-embed')
    const style = document.createElement('style')
    style.textContent = '.office-embed .ai-panel,.office-embed .ai-toggle,.office-embed .shell-home-btn,.office-embed .ai-entry,.office-embed .ribbon-group:has(.ai-entry),.office-embed .ai-fab{display:none!important}'
    document.head.appendChild(style)
  }
  createRoot(document.getElementById('root')!).render(
    <LocaleProvider initial={lang}>
      {embed ? <App /> : <Shell />}
    </LocaleProvider>,
  )
}

void bootstrap()
