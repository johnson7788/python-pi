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
    // `.ai-dock` is the dock column itself; hiding only its children still left the
    // collapsed "AI" handle (`.ai-rail`) pinned to the editor's left edge.
    const style = document.createElement('style')
    style.textContent = '.office-embed .ai-dock,.office-embed .ai-rail,.office-embed .ai-panel,.office-embed .ai-toggle,.office-embed .shell-home-btn,.office-embed .ai-entry,.office-embed .ribbon-group:has(.ai-entry),.office-embed .ai-fab{display:none!important}'
      // The artifact pane is narrow, and a squeezed ribbon overflows instead of
      // helping. Hide it there; the pane's 全屏 button widens the frame past this
      // breakpoint, which brings the ribbon back.
      + '@media (max-width: 1000px){.office-embed .ribbon{display:none!important}}'
    document.head.appendChild(style)
  }
  createRoot(document.getElementById('root')!).render(
    <LocaleProvider initial={lang}>
      {embed ? <App /> : <Shell />}
    </LocaleProvider>,
  )
}

void bootstrap()
