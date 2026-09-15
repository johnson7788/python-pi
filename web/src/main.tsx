import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { applyTheme, readStoredTheme } from './lib/useTheme'
import './styles.css'

// 必须在首次渲染之前落盘，否则锁定的外观会先闪一帧系统默认色。
applyTheme(readStoredTheme())

const container = document.getElementById('root')
if (!container) {
  throw new Error('#root not found in index.html')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
