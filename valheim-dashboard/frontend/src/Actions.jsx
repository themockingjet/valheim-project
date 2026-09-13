import { useCallback, useEffect, useState } from 'react'

const MAX_ROWS = 50

function searchQueryFromInput(value) {
  const input = value.trim()
  if (!input) {
    throw new Error('Enter a mod name or a Hexium package link.')
  }
  if (!input.includes('://')) {
    return input
  }
  let link
  try {
    link = new URL(input)
  } catch {
    throw new Error('Enter a mod name or a valid Hexium package link.')
  }
  const segments = link.pathname.split('/').filter(Boolean)
  if (
    link.protocol !== 'https:' ||
    link.hostname !== 'valheim.hexium.gg' ||
    segments.length !== 3 ||
    segments[0] !== 'mods'
  ) {
    throw new Error('Only Hexium package links from valheim.hexium.gg are accepted.')
  }
  return `${segments[1]} ${segments[2]}`
}

async function submitPendingAction(path, body, csrfToken) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': csrfToken ?? '',
    },
    body: JSON.stringify(body),
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const message =
      payload?.error?.message ?? `Request failed (HTTP ${response.status})`
    throw new Error(message)
  }
  return payload
}

function usePendingStatus(path, refreshToken) {
  const [status, setStatus] = useState({ state: 'loading' })

  useEffect(() => {
    const controller = new AbortController()

    async function load() {
      try {
        const response = await fetch(path, { signal: controller.signal })
        if (!response.ok) {
          throw new Error(`Status request failed (HTTP ${response.status})`)
        }
        const payload = await response.json()
        setStatus({ state: 'ready', ...payload })
      } catch (error) {
        if (error.name !== 'AbortError') {
          setStatus({
            state: 'error',
            message: error instanceof Error ? error.message : 'Status unavailable.',
          })
        }
      }
    }

    load()
    return () => controller.abort()
  }, [path, refreshToken])

  return { status }
}

function ActionResult({ status }) {
  if (status.state === 'loading') {
    return <p className="action-result muted">Checking current state…</p>
  }
  if (status.state === 'error') {
    return <p className="action-result error">{status.message}</p>
  }
  if (status.pending) {
    return (
      <p className="action-result pending">
        Queued — awaiting the privileged helper to consume this request.
      </p>
    )
  }
  if (status.result) {
    const outcome = status.result.outcome ?? 'unknown'
    return (
      <p className={`action-result outcome-${outcome}`}>
        Last outcome: <strong>{outcome}</strong>
        {status.result.message ? ` — ${status.result.message}` : ''}
      </p>
    )
  }
  return <p className="action-result muted">No request has been submitted yet.</p>
}

function packageRows(packages) {
  return packages.map((item) => ({
    namespace: item.namespace,
    name: item.name,
    version: item.version,
    channel: item.channel ?? 'stable',
    role: item.role,
  }))
}

function packageKey({ namespace, name }) {
  return `${namespace}/${name}`
}

function ManifestEditorState({ children }) {
  return (
    <section className="panel actions-panel" aria-labelledby="manifest-editor-heading">
      <div className="section-heading">
        <div>
          <p className="section-eyebrow">Queue for next maintenance</p>
          <h2 id="manifest-editor-heading">Pending Hexium manifest</h2>
        </div>
        <p className="section-description">
          Add, remove, or update packages from the current manifest. Changes are staged for the
          next scheduled maintenance window only and never restart the server on their own.
        </p>
      </div>
      {children}
    </section>
  )
}

export function ManifestEditor({ csrfToken, manifestPackages }) {
  const [refreshToken, setRefreshToken] = useState(0)
  const { status } = usePendingStatus('/api/pending/manifest', refreshToken)
  const refreshPendingStatus = useCallback(
    () => setRefreshToken((current) => current + 1),
    [],
  )

  if (manifestPackages === undefined || status.state === 'loading') {
    return (
      <ManifestEditorState>
        <p className="action-result muted">Loading the current manifest…</p>
      </ManifestEditorState>
    )
  }
  if (status.state === 'error') {
    return (
      <ManifestEditorState>
        <p className="action-result error">
          The queued manifest could not be loaded. Package changes are disabled to avoid replacing
          it.
        </p>
      </ManifestEditorState>
    )
  }
  if (!Array.isArray(manifestPackages)) {
    return (
      <ManifestEditorState>
        <p className="action-result error">
          The current manifest could not be loaded. Package changes are disabled to avoid replacing
          it.
        </p>
      </ManifestEditorState>
    )
  }
  if (status.pending && !Array.isArray(status.request?.packages)) {
    return (
      <ManifestEditorState>
        <p className="action-result error">
          The queued manifest could not be loaded. Package changes are disabled to avoid replacing
          it.
        </p>
      </ManifestEditorState>
    )
  }

  const initialRows = packageRows(status.pending ? status.request.packages : manifestPackages)
  return (
    <ManifestDraftEditor
      key={JSON.stringify(initialRows)}
      csrfToken={csrfToken}
      initialRows={initialRows}
      draftSource={status.pending ? 'Loaded the queued manifest.' : 'Loaded the current manifest.'}
      pendingStatus={status}
      onQueued={refreshPendingStatus}
    />
  )
}

function ManifestDraftEditor({ csrfToken, initialRows, draftSource, pendingStatus, onQueued }) {
  const [rows, setRows] = useState(initialRows)
  const [versionStates, setVersionStates] = useState({})
  const [searchInput, setSearchInput] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searchError, setSearchError] = useState('')
  const [searching, setSearching] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const removeRow = (index) => {
    setRows((current) => current.filter((_, rowIndex) => rowIndex !== index))
  }

  const loadVersions = async (row) => {
    const key = packageKey(row)
    if (versionStates[key]?.state === 'loading' || versionStates[key]?.state === 'ready') {
      return
    }
    setVersionStates((current) => ({ ...current, [key]: { state: 'loading' } }))
    try {
      const response = await fetch(
        `/api/hexium/package/${encodeURIComponent(row.namespace)}/${encodeURIComponent(row.name)}`,
      )
      const payload = await response.json().catch(() => null)
      if (
        !response.ok ||
        payload?.namespace !== row.namespace ||
        payload?.name !== row.name ||
        !Array.isArray(payload?.versions) ||
        payload.versions.length === 0 ||
        !payload.versions.every((version) => typeof version === 'string')
      ) {
        throw new Error(payload?.error?.message ?? 'Package versions are unavailable.')
      }
      setVersionStates((current) => ({
        ...current,
        [key]: { state: 'ready', versions: payload.versions },
      }))
    } catch (error) {
      setVersionStates((current) => ({
        ...current,
        [key]: {
          state: 'error',
          message: error instanceof Error ? error.message : 'Package versions are unavailable.',
        },
      }))
    }
  }

  const changeVersion = (index, version) => {
    setRows((current) =>
      current.map((row, rowIndex) => (rowIndex === index ? { ...row, version } : row)),
    )
  }

  const searchPackages = async (event) => {
    event.preventDefault()
    setSearchError('')
    setSearchResults([])
    setSearching(true)
    try {
      const query = searchQueryFromInput(searchInput)
      const response = await fetch(`/api/hexium/search?q=${encodeURIComponent(query)}`)
      const payload = await response.json().catch(() => null)
      if (!response.ok || !Array.isArray(payload?.results)) {
        throw new Error(payload?.error?.message ?? 'Hexium search is unavailable.')
      }
      setSearchResults(payload.results)
    } catch (error) {
      setSearchError(error instanceof Error ? error.message : 'Hexium search is unavailable.')
    } finally {
      setSearching(false)
    }
  }

  const addSearchResult = (result) => {
    setRows((current) => {
      const index = current.findIndex(
        (row) => row.namespace === result.namespace && row.name === result.name,
      )
      if (index === -1 && current.length >= MAX_ROWS) {
        return current
      }
      const selectedRow = {
        namespace: result.namespace,
        name: result.name,
        version: result.latest_version,
        channel: 'stable',
        role: 'server',
      }
      if (index === -1) {
        return [...current, selectedRow]
      }
      return current.map((row, rowIndex) => (rowIndex === index ? selectedRow : row))
    })
  }

  const submit = async (event) => {
    event.preventDefault()
    setSubmitError('')
    setSubmitting(true)
    try {
      await submitPendingAction('/api/pending/manifest', { packages: rows }, csrfToken)
      onQueued()
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : 'Submission failed.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <ManifestEditorState>
      <form onSubmit={searchPackages} className="package-search">
        <label htmlFor="hexium-search">Find a Hexium mod</label>
        <div className="package-search-controls">
          <input
            id="hexium-search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="Search by name or paste a Hexium package link"
            maxLength={512}
          />
          <button type="submit" disabled={searching}>
            {searching ? 'Searching…' : 'Search'}
          </button>
        </div>
        <p className="search-help">
          Search returns matches first. Choose <strong>Add to manifest</strong> on the mod you
          want; pasting a Hexium package link only searches for it. After adding a package, use
          <strong> Choose version</strong> below to select an active Hexium version.
        </p>
      </form>
      {searchError && <p className="action-result error">{searchError}</p>}
      {searchResults.length > 0 && (
        <div className="search-results" aria-live="polite">
          <h3>Matched Hexium packages</h3>
          {searchResults.map((result) => {
            const isAdded = rows.some(
              (row) => row.namespace === result.namespace && row.name === result.name,
            )
            const selectedVersion = rows.find(
              (row) => row.namespace === result.namespace && row.name === result.name,
            )?.version
            const isCurrentVersion = selectedVersion === result.latest_version
            return (
              <article className="search-result" key={`${result.namespace}-${result.name}`}>
                <div>
                  <strong>{result.name}</strong>
                  <span>{result.namespace} · latest {result.latest_version}</span>
                  {result.description && <p>{result.description}</p>}
                  <small>{result.dependency_count} declared dependencies</small>
                </div>
                <button
                  type="button"
                  onClick={() => addSearchResult(result)}
                  disabled={
                    (isAdded && isCurrentVersion) ||
                    (!isAdded && rows.length >= MAX_ROWS)
                  }
                >
                  {isAdded ? (isCurrentVersion ? 'Already selected' : 'Update to latest') : 'Add to manifest'}
                </button>
              </article>
            )
          })}
        </div>
      )}

      <form onSubmit={submit} className="manifest-form">
        <section className="upcoming-manifest" aria-labelledby="upcoming-manifest-heading">
          <div className="upcoming-manifest-heading">
            <div>
              <p className="section-eyebrow">Editable draft</p>
              <h3 id="upcoming-manifest-heading">Managed manifest packages</h3>
            </div>
            <span>{rows.length} selected</span>
          </div>
          <p className="manifest-draft-help">
            These declared packages will be installed, changed, or uninstalled at the next
            maintenance run. Dependencies are resolved automatically and cannot be removed
            independently.
          </p>
          {rows.length === 0 ? (
            <p className="upcoming-manifest-empty">No packages are selected.</p>
          ) : (
            <ul className="upcoming-package-list">
              {rows.map((row, index) => {
                const versionState = versionStates[packageKey(row)]
                const availableVersions = versionState?.versions ?? []
                const selectableVersions = availableVersions.includes(row.version)
                  ? availableVersions
                  : [row.version, ...availableVersions]
                return (
                  <li className="upcoming-package" key={`${row.namespace}-${row.name}`}>
                    <div>
                      <strong>{row.name}</strong>
                      <span>
                        {row.namespace} · version {row.version}
                      </span>
                      <small>Hexium · {row.channel} channel · {row.role}</small>
                    </div>
                    <div className="upcoming-package-actions">
                      {versionState?.state === 'ready' ? (
                        <label className="version-select">
                          <span>Version</span>
                          <select
                            value={row.version}
                            onChange={(event) => changeVersion(index, event.target.value)}
                          >
                            {selectableVersions.map((version) => (
                              <option key={version} value={version}>
                                {version}
                                {!availableVersions.includes(version) ? ' (currently selected)' : ''}
                              </option>
                            ))}
                          </select>
                        </label>
                      ) : (
                        <button
                          type="button"
                          className="row-version"
                          onClick={() => loadVersions(row)}
                          disabled={versionState?.state === 'loading'}
                        >
                          {versionState?.state === 'loading'
                            ? 'Loading versions…'
                            : 'Choose version'}
                        </button>
                      )}
                      <button
                        type="button"
                        className="row-remove"
                        onClick={() => removeRow(index)}
                        aria-label={`Uninstall ${row.name} at the next maintenance run`}
                      >
                        Uninstall
                      </button>
                    </div>
                    {versionState?.state === 'error' && (
                      <p className="package-version-error" role="alert">
                        {versionState.message}
                      </p>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </section>
        <div className="manifest-form-actions">
          <button
            type="submit"
            className="primary"
            disabled={submitting || !csrfToken || rows.length === 0}
          >
            {submitting ? 'Submitting…' : 'Queue manifest for maintenance'}
          </button>
        </div>
      </form>
      {draftSource && <p className="action-result muted">{draftSource}</p>}
      {submitError && <p className="action-result error">{submitError}</p>}
      <ActionResult status={pendingStatus} />
    </ManifestEditorState>
  )
}

function ConfirmedAction({ id, title, description, path, confirmationWord, csrfToken }) {
  const [confirmationText, setConfirmationText] = useState('')
  const [reason, setReason] = useState('')
  const [submitError, setSubmitError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)
  const { status } = usePendingStatus(path, refreshToken)

  const submit = async (event) => {
    event.preventDefault()
    setSubmitError('')
    setSubmitting(true)
    try {
      await submitPendingAction(path, { confirmation: confirmationText, reason }, csrfToken)
      setConfirmationText('')
      setReason('')
      setRefreshToken((current) => current + 1)
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : 'Submission failed.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="panel actions-panel" aria-labelledby={`${id}-heading`}>
      <div className="section-heading">
        <div>
          <p className="section-eyebrow">Scoped, confirmed action</p>
          <h2 id={`${id}-heading`}>{title}</h2>
        </div>
        <p className="section-description">{description}</p>
      </div>
      <form onSubmit={submit} className="confirm-form">
        <label htmlFor={`${id}-confirm`}>
          Type <code>{confirmationWord}</code> to confirm
        </label>
        <input
          id={`${id}-confirm`}
          value={confirmationText}
          onChange={(event) => setConfirmationText(event.target.value)}
          autoComplete="off"
        />
        <label htmlFor={`${id}-reason`}>Reason (optional)</label>
        <input
          id={`${id}-reason`}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={200}
        />
        <button
          type="submit"
          className="primary danger"
          disabled={submitting || !csrfToken || confirmationText !== confirmationWord}
        >
          {submitting ? 'Submitting…' : title}
        </button>
      </form>
      {submitError && <p className="action-result error">{submitError}</p>}
      <ActionResult status={status} />
    </section>
  )
}

export function UpdateAction({ csrfToken }) {
  return (
    <ConfirmedAction
      id="update-action"
      title="Run maintenance now"
      description="Runs the same full workflow as scheduled maintenance: validates the server install,
        resolves and activates the current manifest, then restarts Valheim with a ready health check."
      path="/api/pending/update"
      confirmationWord="UPDATE"
      csrfToken={csrfToken}
    />
  )
}

export function RollbackAction({ csrfToken }) {
  return (
    <ConfirmedAction
      id="rollback-action"
      title="Roll back release"
      description="Submits a scoped rollback-only request that activates the previous known-good
        release and restarts the server, with a health check before reporting success."
      path="/api/pending/rollback"
      confirmationWord="ROLLBACK"
      csrfToken={csrfToken}
    />
  )
}

function formatBackupSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) {
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

function formatBackupTime(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return 'Unknown time'
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Shanghai',
  }).format(date)
}

export function WorldBackupManager({ csrfToken }) {
  const [inventory, setInventory] = useState(null)
  const [inventoryError, setInventoryError] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [confirmationText, setConfirmationText] = useState('')
  const [reason, setReason] = useState('')
  const [submitError, setSubmitError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)
  const { status } = usePendingStatus('/api/pending/world-restore', refreshToken)

  useEffect(() => {
    const controller = new AbortController()

    async function load() {
      setInventoryError('')
      try {
        const response = await fetch('/api/world-backups', { signal: controller.signal })
        if (!response.ok) {
          throw new Error('World backup inventory is unavailable.')
        }
        const payload = await response.json()
        if (!Array.isArray(payload.backups) || !payload.active_world) {
          throw new Error('World backup inventory is invalid.')
        }
        setInventory(payload)
      } catch (error) {
        if (error.name !== 'AbortError') {
          setInventoryError(
            error instanceof Error ? error.message : 'World backup inventory is unavailable.',
          )
        }
      }
    }

    load()
    return () => controller.abort()
  }, [refreshToken])

  const submit = async (event) => {
    event.preventDefault()
    setSubmitError('')
    setSubmitting(true)
    try {
      await submitPendingAction(
        '/api/pending/world-restore',
        { backup_id: selectedId, confirmation: confirmationText, reason },
        csrfToken,
      )
      setConfirmationText('')
      setReason('')
      setRefreshToken((current) => current + 1)
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : 'Restore submission failed.')
    } finally {
      setSubmitting(false)
    }
  }

  const readyBackups = inventory?.backups?.filter((backup) => backup.integrity === 'ready') ?? []

  return (
    <section className="panel actions-panel world-backups-panel" aria-labelledby="world-backups-heading">
      <div className="section-heading">
        <div>
          <p className="section-eyebrow">Root-exported inventory</p>
          <h2 id="world-backups-heading">World backups</h2>
        </div>
        <p className="section-description">
          Native Valheim backups are complete world directories. Restoring one replaces the
          active world only after a graceful stop, then verifies the next server start.
        </p>
      </div>

      {inventoryError && <p className="action-result error">{inventoryError}</p>}
      {inventory && (
        <>
          <dl className="world-backup-summary">
            <div>
              <dt>Active world</dt>
              <dd>{inventory.active_world.integrity === 'ready' ? 'Ready' : 'Unavailable'}</dd>
            </div>
            <div>
              <dt>Active size</dt>
              <dd>{formatBackupSize(inventory.active_world.size_bytes)}</dd>
            </div>
            <div>
              <dt>Native backups</dt>
              <dd>{inventory.native_retention.observed_count}</dd>
            </div>
          </dl>
          {readyBackups.length ? (
            <div className="table-wrap">
              <table>
                <caption className="sr-only">Available native world backups</caption>
                <thead>
                  <tr>
                    <th scope="col">Created</th>
                    <th scope="col">Size</th>
                    <th scope="col">Files</th>
                    <th scope="col">Status</th>
                    <th scope="col"><span className="sr-only">Selection</span></th>
                  </tr>
                </thead>
                <tbody>
                  {readyBackups.map((backup) => (
                    <tr key={backup.id} className={selectedId === backup.id ? 'selected-backup' : ''}>
                      <td>{formatBackupTime(backup.created_at)}</td>
                      <td>{formatBackupSize(backup.size_bytes)}</td>
                      <td>{backup.file_count}</td>
                      <td><span className="flag">Ready</span></td>
                      <td>
                        <button type="button" onClick={() => setSelectedId(backup.id)}>
                          {selectedId === backup.id ? 'Selected' : 'Select'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="empty-state">No restorable native world backups are currently available.</p>
          )}
        </>
      )}

      <form onSubmit={submit} className="confirm-form">
        <label htmlFor="world-restore-confirm">
          Type <code>RESTORE</code> to restore the selected native backup
        </label>
        <input
          id="world-restore-confirm"
          value={confirmationText}
          onChange={(event) => setConfirmationText(event.target.value)}
          autoComplete="off"
        />
        <label htmlFor="world-restore-reason">Reason (optional)</label>
        <input
          id="world-restore-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={200}
        />
        <button
          type="submit"
          className="primary danger"
          disabled={
            submitting || !csrfToken || !selectedId || confirmationText !== 'RESTORE'
          }
        >
          {submitting ? 'Submitting…' : 'Restore selected world backup'}
        </button>
      </form>
      {submitError && <p className="action-result error">{submitError}</p>}
      <ActionResult status={status} />
    </section>
  )
}

export function AuditTrail() {
  const [events, setEvents] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()

    async function load() {
      try {
        const response = await fetch('/api/audit', { signal: controller.signal })
        if (!response.ok) {
          throw new Error(`Audit request failed (HTTP ${response.status})`)
        }
        const payload = await response.json()
        setEvents(Array.isArray(payload.events) ? payload.events.slice(-10).reverse() : [])
      } catch (fetchError) {
        if (fetchError.name !== 'AbortError') {
          setError(
            fetchError instanceof Error ? fetchError.message : 'Audit trail unavailable.',
          )
        }
      }
    }

    load()
    return () => controller.abort()
  }, [])

  return (
    <section className="panel actions-panel" aria-labelledby="audit-heading">
      <div className="section-heading">
        <div>
          <p className="section-eyebrow">Bounded audit trail</p>
          <h2 id="audit-heading">Recent operator actions</h2>
        </div>
      </div>
      {error && <p className="action-result error">{error}</p>}
      {events.length ? (
        <ul className="audit-list">
          {events.map((event, index) => (
            <li key={index} className={`audit-item audit-item--${event.outcome}`}>
              <span className="audit-action">{event.action}</span>
              <span className="audit-outcome">{event.outcome}</span>
              <span className="audit-time">{event.recorded_at}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty-state">No operator actions have been recorded yet.</p>
      )}
    </section>
  )
}
