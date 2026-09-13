import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AuditTrail,
  ManifestEditor,
  RestartAction,
  RollbackAction,
  WorldBackupManager,
} from './Actions'
import './App.css'
import { useDashboardSession } from './useDashboardSession'

const STALE_AFTER_MS = 5 * 60 * 1000
const LOG_AUTO_FOLLOW_RESUME_MS = 5 * 1000

const emptyStatus = {
  state: 'loading',
  data: null,
  message: '',
  loadedAt: null,
  refreshing: false,
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isNullableNumber(value) {
  return value === null || (typeof value === 'number' && Number.isFinite(value))
}

function isNullableString(value) {
  return value === null || typeof value === 'string'
}

function isStatusSnapshot(value) {
  if (
    !isRecord(value) ||
    value.schema_version !== 1 ||
    typeof value.generated_at !== 'string' ||
    !Number.isFinite(Date.parse(value.generated_at))
  ) {
    return false
  }

  const server = value.server
  const maintenance = value.maintenance
  const maintenanceLast = maintenance?.last
  const modpack = value.modpack
  const logs = value.logs

  return (
    isRecord(server) &&
    typeof server.active_state === 'string' &&
    typeof server.sub_state === 'string' &&
    isNullableNumber(server.pid) &&
    isNullableNumber(server.uptime_seconds) &&
    isNullableNumber(server.memory_current_bytes) &&
    isNullableNumber(server.cpu_usage_ns) &&
    isRecord(maintenance) &&
    isNullableString(maintenance.next_scheduled_at) &&
    isRecord(maintenanceLast) &&
    typeof maintenanceLast.outcome === 'string' &&
    isNullableString(maintenanceLast.started_at) &&
    isNullableString(maintenanceLast.completed_at) &&
    typeof maintenanceLast.rollback_performed === 'boolean' &&
    isNullableString(maintenanceLast.failure_reason) &&
    isRecord(modpack) &&
    isNullableString(modpack.active_release) &&
    isNullableString(modpack.previous_release) &&
    Array.isArray(modpack.packages) &&
    modpack.packages.every(
      (item) =>
        isRecord(item) &&
        typeof item.namespace === 'string' &&
        typeof item.name === 'string' &&
        typeof item.version === 'string' &&
        (item.role === 'server' || item.role === 'client'),
    ) &&
    isRecord(logs) &&
    Array.isArray(logs.server) &&
    logs.server.every((line) => typeof line === 'string') &&
    Array.isArray(logs.maintenance) &&
    logs.maintenance.every((line) => typeof line === 'string')
  )
}

async function getErrorMessage(response) {
  try {
    const payload = await response.json()
    if (isRecord(payload?.error) && typeof payload.error.message === 'string') {
      return payload.error.message
    }
  } catch (error) {
    if (!(error instanceof SyntaxError)) {
      throw error
    }
  }

  return `Status snapshot unavailable (HTTP ${response.status})`
}

function useStatusSnapshot(refreshToken) {
  const [status, setStatus] = useState(emptyStatus)

  useEffect(() => {
    const controller = new AbortController()
    let active = true

    async function loadStatus() {
      setStatus((current) =>
        current.data
          ? { ...current, message: '', refreshing: true }
          : { ...emptyStatus },
      )

      try {
        const response = await fetch('/api/status', {
          signal: controller.signal,
        })

        if (!response.ok) {
          throw new Error(await getErrorMessage(response))
        }

        const payload = await response.json()
        if (!isStatusSnapshot(payload)) {
          throw new Error('The backend returned an invalid status snapshot.')
        }

        if (active) {
          setStatus({
            state: 'ready',
            data: payload,
            message: '',
            loadedAt: Date.now(),
            refreshing: false,
          })
        }
      } catch (error) {
        if (error.name !== 'AbortError' && active) {
          const message =
            error instanceof Error ? error.message : 'The status snapshot could not be loaded.'
          setStatus((current) =>
            current.data
              ? { ...current, message, refreshing: false }
              : {
                  state: 'error',
                  data: null,
                  message,
                  loadedAt: null,
                  refreshing: false,
                },
          )
        }
      }
    }

    loadStatus()
    return () => {
      active = false
      controller.abort()
    }
  }, [refreshToken])

  return status
}

function useStatusEvents(refresh) {
  useEffect(() => {
    const events = new EventSource('/api/status/events')
    events.addEventListener('snapshot', refresh)
    return () => events.close()
  }, [refresh])
}

function formatLabel(value) {
  return value.replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function formatTimestamp(value, timeZone) {
  if (!value) {
    return 'Not recorded'
  }

  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return 'Invalid timestamp'
  }

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
    ...(timeZone ? { timeZone } : {}),
  }).format(date) + (timeZone ? ` (${timeZone})` : '')
}

function formatAge(value) {
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 1000))
  if (elapsedSeconds < 60) {
    return 'just now'
  }

  const minutes = Math.floor(elapsedSeconds / 60)
  if (minutes < 60) {
    return `${minutes} min ago`
  }

  const hours = Math.floor(minutes / 60)
  return `${hours} hr ago`
}

function formatDuration(seconds) {
  if (seconds === null) {
    return 'Not reported'
  }

  const totalSeconds = Math.max(0, Math.floor(seconds))
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const remainingSeconds = totalSeconds % 60
  const parts = []

  if (days) parts.push(`${days}d`)
  if (hours || days) parts.push(`${hours}h`)
  if (minutes || hours || days) parts.push(`${minutes}m`)
  parts.push(`${remainingSeconds}s`)
  return parts.join(' ')
}

function formatBytes(bytes) {
  if (bytes === null) {
    return 'Not reported'
  }

  if (bytes < 1024) {
    return `${bytes} B`
  }

  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unitIndex = -1
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }

  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`
}

function formatCpuTime(nanoseconds) {
  if (nanoseconds === null) {
    return 'Not reported'
  }

  return `${(nanoseconds / 1e9).toFixed(nanoseconds >= 1e9 ? 2 : 3)} s`
}

function Value({ children, muted = false }) {
  return <span className={muted ? 'value muted' : 'value'}>{children}</span>
}

function Metric({ label, children }) {
  return (
    <div className="metric">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function SectionHeading({ eyebrow, title, description, headingId }) {
  return (
    <div className="section-heading">
      <div>
        <p className="section-eyebrow">{eyebrow}</p>
        <h2 id={headingId}>{title}</h2>
      </div>
      {description && <p className="section-description">{description}</p>}
    </div>
  )
}

function EmptyState({ children }) {
  return <p className="empty-state">{children}</p>
}

function LogPanel({ title, lines }) {
  const outputRef = useRef(null)
  const resumeTimerRef = useRef(null)
  const isFollowingRef = useRef(true)

  const clearResumeTimer = useCallback(() => {
    if (resumeTimerRef.current !== null) {
      clearTimeout(resumeTimerRef.current)
      resumeTimerRef.current = null
    }
  }, [])

  const followLatest = useCallback(() => {
    const output = outputRef.current
    if (output) {
      output.scrollTop = output.scrollHeight
    }
  }, [])

  const pauseFollowing = useCallback(() => {
    isFollowingRef.current = false
    clearResumeTimer()
    resumeTimerRef.current = setTimeout(() => {
      isFollowingRef.current = true
      resumeTimerRef.current = null
      followLatest()
    }, LOG_AUTO_FOLLOW_RESUME_MS)
  }, [clearResumeTimer, followLatest])

  const handleScroll = useCallback(
    (event) => {
      const output = event.currentTarget
      const isAtBottom = output.scrollHeight - output.clientHeight - output.scrollTop <= 1

      if (isAtBottom) {
        isFollowingRef.current = true
        clearResumeTimer()
        return
      }

      pauseFollowing()
    },
    [clearResumeTimer, pauseFollowing],
  )

  useEffect(() => {
    if (lines.length && isFollowingRef.current) {
      followLatest()
    }
  }, [followLatest, lines])

  useEffect(() => clearResumeTimer, [clearResumeTimer])

  return (
    <div className="log-panel">
      <h3>{title}</h3>
      {lines.length ? (
        <pre
          ref={outputRef}
          aria-label={`${title} log output`}
          onScroll={handleScroll}
          onTouchMove={pauseFollowing}
          onWheel={pauseFollowing}
        >
          {lines.join('\n')}
        </pre>
      ) : (
        <EmptyState>No entries in this snapshot.</EmptyState>
      )}
    </div>
  )
}

function BackendHealth() {
  const [health, setHealth] = useState({ state: 'loading' })

  useEffect(() => {
    const controller = new AbortController()

    async function loadHealth() {
      try {
        const response = await fetch('/api/healthz', {
          signal: controller.signal,
        })

        if (!response.ok) {
          throw new Error(`Backend returned HTTP ${response.status}`)
        }

        const payload = await response.json()
        setHealth({ state: 'ready', status: payload.status })
      } catch (error) {
        if (error.name !== 'AbortError') {
          setHealth({ state: 'error', message: error.message })
        }
      }
    }

    loadHealth()
    return () => controller.abort()
  }, [])

  return (
    <section className="panel health-card" aria-live="polite">
      <div className="panel-heading">
        <div>
          <p className="section-eyebrow">Connectivity</p>
          <h2>Backend connection</h2>
        </div>
        <span className={`status-dot status-dot--${health.state}`} aria-hidden="true" />
      </div>
      {health.state === 'loading' && (
        <p className="status-message" role="status">
          Checking localhost backend…
        </p>
      )}
      {health.state === 'ready' && (
        <p className="status-message success">
          <strong>Connected:</strong>
          <span>{health.status}</span>
        </p>
      )}
      {health.state === 'error' && (
        <p className="status-message error">
          <strong>Unavailable:</strong>
          <span>{health.message}</span>
        </p>
      )}
    </section>
  )
}

function SnapshotState({ status, onRetry }) {
  if (status.state === 'loading') {
    return (
      <section className="panel state-panel" aria-live="polite" role="status">
        <div className="loading-mark" aria-hidden="true" />
        <div>
          <h2>Loading operations snapshot</h2>
          <p>Reading the latest server, maintenance, and modpack state…</p>
        </div>
      </section>
    )
  }

  return (
    <section className="panel state-panel state-panel--error" role="alert">
      <div className="state-icon" aria-hidden="true">
        !
      </div>
      <div>
        <h2>Operations snapshot unavailable</h2>
        <p>{status.message}</p>
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      </div>
    </section>
  )
}

function OperationsOverview({ snapshot, loadedAt, refreshError, refreshing }) {
  const { server, maintenance, modpack, logs } = snapshot
  const isStale =
    loadedAt !== null && loadedAt - Date.parse(snapshot.generated_at) > STALE_AFTER_MS

  return (
    <div className="operations-snapshot" aria-busy={refreshing}>
      {isStale && (
        <div className="stale-banner" role="status">
          <span aria-hidden="true">◷</span>
          <div>
            <strong>Stale snapshot</strong>
            <span>
              Generated {formatAge(snapshot.generated_at)}. Live controls are intentionally
              unavailable in this read-only view.
            </span>
          </div>
        </div>
      )}
      {refreshError && (
        <p className="snapshot-refresh-error" role="status">
          Showing the last successful snapshot. {refreshError}
        </p>
      )}

      <section className="panel overview-panel" aria-labelledby="overview-heading">
        <SectionHeading
          eyebrow="Read-only snapshot"
          title="Operations overview"
          description={`Captured ${formatTimestamp(snapshot.generated_at)}`}
          headingId="overview-heading"
        />

        <div className="server-summary">
          <div className="server-state">
            <span className="state-label">Active state</span>
            <strong>{formatLabel(server.active_state)}</strong>
            <span className="sub-state">{formatLabel(server.sub_state)}</span>
          </div>
          <dl className="metric-grid">
            <Metric label="Process ID">
              <Value muted={server.pid === null}>{server.pid ?? 'Not running'}</Value>
            </Metric>
            <Metric label="Uptime">
              <Value muted={server.uptime_seconds === null}>
                {formatDuration(server.uptime_seconds)}
              </Value>
            </Metric>
            <Metric label="Memory">
              <Value muted={server.memory_current_bytes === null}>
                {formatBytes(server.memory_current_bytes)}
              </Value>
            </Metric>
            <Metric label="CPU time">
              <Value muted={server.cpu_usage_ns === null}>
                {formatCpuTime(server.cpu_usage_ns)}
              </Value>
            </Metric>
          </dl>
        </div>
      </section>

      <div className="content-grid">
        <section className="panel maintenance-panel" aria-labelledby="maintenance-heading">
          <SectionHeading
            eyebrow="Scheduled work"
            title="Maintenance"
            description="Latest known maintenance activity"
            headingId="maintenance-heading"
          />
          <dl className="detail-list">
            <div>
              <dt>Next scheduled</dt>
              <dd>
                {maintenance.next_scheduled_at ? (
                  <Value>{formatTimestamp(maintenance.next_scheduled_at, 'Asia/Shanghai')}</Value>
                ) : (
                  <Value muted>Not scheduled</Value>
                )}
              </dd>
            </div>
            <div>
              <dt>Last outcome</dt>
              <dd>
                {maintenance.last.outcome ? (
                  <span className={`outcome outcome--${maintenance.last.outcome.toLowerCase()}`}>
                    {formatLabel(maintenance.last.outcome)}
                  </span>
                ) : (
                  <Value muted>Not recorded</Value>
                )}
              </dd>
            </div>
            <div>
              <dt>Started</dt>
              <dd>
                <Value muted={!maintenance.last.started_at}>
                  {formatTimestamp(maintenance.last.started_at)}
                </Value>
              </dd>
            </div>
            <div>
              <dt>Completed</dt>
              <dd>
                <Value muted={!maintenance.last.completed_at}>
                  {formatTimestamp(maintenance.last.completed_at)}
                </Value>
              </dd>
            </div>
            <div>
              <dt>Rollback</dt>
              <dd>
                <span
                  className={
                    maintenance.last.rollback_performed ? 'flag flag--warning' : 'flag'
                  }
                >
                  {maintenance.last.rollback_performed ? 'Performed' : 'Not performed'}
                </span>
              </dd>
            </div>
          </dl>
          {maintenance.last.failure_reason && (
            <div className="failure-note">
              <span>Failure reason</span>
              <p>{maintenance.last.failure_reason}</p>
            </div>
          )}
        </section>

        <section className="panel modpack-panel" aria-labelledby="modpack-heading">
          <SectionHeading
            eyebrow="Release inventory"
            title="Modpack"
            description="Packages included in the active snapshot"
            headingId="modpack-heading"
          />
          <div className="release-pair">
            <div>
              <span className="state-label">Active release</span>
              <strong>{modpack.active_release || 'No active release'}</strong>
            </div>
            <div>
              <span className="state-label">Previous release</span>
              <strong className={!modpack.previous_release ? 'muted' : ''}>
                {modpack.previous_release || 'Not recorded'}
              </strong>
            </div>
          </div>
          {modpack.packages.length ? (
            <div className="table-wrap">
              <table>
                <caption className="sr-only">Modpack package inventory</caption>
                <thead>
                  <tr>
                    <th scope="col">Package</th>
                    <th scope="col">Version</th>
                    <th scope="col">Role</th>
                  </tr>
                </thead>
                <tbody>
                  {modpack.packages.map((item) => (
                    <tr key={`${item.namespace}/${item.name}/${item.version}/${item.role}`}>
                      <td>
                        <strong>{item.name}</strong>
                        <span>{item.namespace}</span>
                      </td>
                      <td>{item.version}</td>
                      <td>
                        <span className="role-tag">{item.role}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState>No packages are recorded in this snapshot.</EmptyState>
          )}
        </section>
      </div>

      <section className="panel logs-panel" aria-labelledby="logs-heading">
        <SectionHeading
          eyebrow="Recent output"
          title="Logs"
          description="Text captured in the status snapshot"
          headingId="logs-heading"
        />
        <div className="logs-grid">
          <LogPanel title="Server log" lines={logs.server} />
          <LogPanel title="Maintenance log" lines={logs.maintenance} />
        </div>
      </section>
    </div>
  )
}

function App() {
  const [refreshToken, setRefreshToken] = useState(0)
  const status = useStatusSnapshot(refreshToken)
  const { csrfToken, error: sessionError } = useDashboardSession()
  const refreshStatus = useCallback(() => setRefreshToken((current) => current + 1), [])
  useStatusEvents(refreshStatus)

  return (
    <main className="app-shell">
      <header className="hero">
        <div className="hero-copy">
          <p className="eyebrow">Local operator console</p>
          <h1>Valheim Dashboard</h1>
          <p className="hero-description">
            A calm view of server health, maintenance activity, and the active modpack
            release, with scoped and confirmed operator actions.
          </p>
        </div>
        <div className="read-only-badge">
          <span className="lock-mark" aria-hidden="true">
            ◇
          </span>
          Same-origin only
        </div>
      </header>

      <BackendHealth />

      {status.state === 'ready' ? (
        <OperationsOverview
          snapshot={status.data}
          loadedAt={status.loadedAt}
          refreshError={status.message}
          refreshing={status.refreshing}
        />
      ) : (
        <SnapshotState
          status={status}
          onRetry={() => setRefreshToken((current) => current + 1)}
        />
      )}

      {sessionError && <p className="action-result error">{sessionError}</p>}
      <ManifestEditor csrfToken={csrfToken} />
      <div className="content-grid">
        <RestartAction csrfToken={csrfToken} />
        <RollbackAction csrfToken={csrfToken} />
      </div>
      <WorldBackupManager csrfToken={csrfToken} />
      <AuditTrail />

      <footer>
        <span>Valheim operations console</span>
        <span>Same-origin snapshot and action API</span>
      </footer>
    </main>
  )
}

export default App
