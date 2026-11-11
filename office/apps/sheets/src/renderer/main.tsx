import './web-adapter'

import ReactDOM from 'react-dom/client'
import { htmlLang, type Lang } from '@genoffice/i18n'

import '@univerjs/preset-sheets-core/lib/index.css'

import { App } from './App'
import { LocaleProvider, setModuleLang } from './i18n/locale'
import './styles.css'

if (import.meta.hot) {
  import.meta.hot.on('vite:beforeUpdate', ({ updates }) => {
    const replacesUniverRuntime = updates.some(
      ({ path }) => path.endsWith('/App.tsx') || path.endsWith('/univer-sync.ts'),
    )
    if (replacesUniverRuntime) window.location.reload()
  })
}

const embed = new URLSearchParams(location.search).get('embed') === '1'
if (embed) {
  document.body.classList.add('office-embed')
  const style = document.createElement('style')
  style.textContent = '.office-embed .copilot,.office-embed .ai-panel-resizer,.office-embed .ai-entry,.office-embed .ribbon-group:has(.ai-entry){display:none!important}.office-embed .sheet-body{grid-template-columns:minmax(0,1fr)!important}'
    + '@media (max-width: 1000px){.office-embed .ribbon{display:none!important}}'
  document.head.appendChild(style)
}

const root = document.getElementById('root')
if (!root) throw new Error('Missing application root.')

async function bootstrap(): Promise<void> {
  let lang: Lang = 'zh'
  try {
    lang = await window.desktopApi.getLanguage()
  } catch {
    /* dev renderer without the preload handler */
  }
  setModuleLang(lang)
  document.documentElement.lang = htmlLang(lang)
  ReactDOM.createRoot(root!).render(
    <LocaleProvider initial={lang}>
      <App />
    </LocaleProvider>,
  )
}

void bootstrap()
