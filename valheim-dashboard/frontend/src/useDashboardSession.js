import { useCallback, useEffect, useState } from 'react'

/**
 * Establishes the anti-CSRF dashboard session used by every state-changing
 * action. The cookie itself is set by the browser from the response; this
 * hook only needs to remember the token value to echo back in headers.
 */
export function useDashboardSession() {
  const [csrfToken, setCsrfToken] = useState(null)
  const [error, setError] = useState('')
  const [refreshToken, setRefreshToken] = useState(0)

  useEffect(() => {
    const controller = new AbortController()

    async function loadSession() {
      try {
        const response = await fetch('/api/session', { signal: controller.signal })
        if (!response.ok) {
          throw new Error(`Session request failed (HTTP ${response.status})`)
        }
        const payload = await response.json()
        if (typeof payload.csrf_token !== 'string' || !payload.csrf_token) {
          throw new Error('Session response was missing a CSRF token.')
        }
        setCsrfToken(payload.csrf_token)
        setError('')
      } catch (fetchError) {
        if (fetchError.name !== 'AbortError') {
          setError(
            fetchError instanceof Error ? fetchError.message : 'Could not start a session.',
          )
        }
      }
    }

    loadSession()
    return () => controller.abort()
  }, [refreshToken])

  const refresh = useCallback(() => setRefreshToken((current) => current + 1), [])

  return { csrfToken, error, refresh }
}
