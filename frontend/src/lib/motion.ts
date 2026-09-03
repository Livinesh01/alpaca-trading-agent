import { useEffect, useState } from 'react'

const QUERY = '(prefers-reduced-motion: reduce)'

function readPreference(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia(QUERY).matches
  } catch {
    return false
  }
}

/**
 * Reactive `prefers-reduced-motion` flag with a jsdom-safe fallback.
 * Components use it to skip JS-driven animation loops entirely.
 */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(readPreference)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia(QUERY)
    const onChange = () => setReduced(mq.matches)
    onChange()
    mq.addEventListener?.('change', onChange)
    return () => {
      mq.removeEventListener?.('change', onChange)
    }
  }, [])
  return reduced
}

/** requestAnimationFrame with a setTimeout fallback (older jsdom). */
export function raf(callback: (time: number) => void): number {
  if (typeof requestAnimationFrame === 'function') return requestAnimationFrame(callback)
  return window.setTimeout(() => callback(performance.now()), 16)
}

/** Cancels a handle returned by `raf`. */
export function cancelRaf(handle: number): void {
  if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(handle)
  else clearTimeout(handle)
}