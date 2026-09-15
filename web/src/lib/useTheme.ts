/**
 * 外观：默认跟随系统，用户可以手动锁定。
 *
 * 存下来的选择在 React 渲染之前就写到 `<html data-theme>`（见 `main.tsx`），
 * 所以强制深色时不会先闪一下浅色。没有存值时属性直接缺省，交给 CSS 的
 * `prefers-color-scheme` 决定——从没点过开关的人不需要知道这个开关存在。
 */

import { useCallback, useEffect, useState } from 'react'

export type ThemeMode = 'light' | 'dark'

const STORAGE_KEY = 'pi-theme'

export function readStoredTheme(): ThemeMode | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    // 隐私模式 / storage 被禁用：退回系统偏好。
    return null
  }
}

export function applyTheme(mode: ThemeMode | null): void {
  const root = document.documentElement
  if (mode) root.dataset.theme = mode
  else delete root.dataset.theme
}

function systemPrefersDark(): boolean {
  return (
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  )
}

export function useTheme(): { isDark: boolean; toggle: () => void } {
  const [mode, setMode] = useState<ThemeMode | null>(readStoredTheme)
  const [systemDark, setSystemDark] = useState(systemPrefersDark)

  // 只有「跟随系统」时才需要监听变化。
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const sync = () => setSystemDark(query.matches)
    sync()
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [])

  const isDark = mode ? mode === 'dark' : systemDark

  const toggle = useCallback(() => {
    setMode((current) => {
      const resolved = current ?? (systemPrefersDark() ? 'dark' : 'light')
      const next: ThemeMode = resolved === 'dark' ? 'light' : 'dark'
      applyTheme(next)
      try {
        window.localStorage.setItem(STORAGE_KEY, next)
      } catch {
        // 存不了就只保留在内存里，本次会话仍然生效。
      }
      return next
    })
  }, [])

  return { isDark, toggle }
}
