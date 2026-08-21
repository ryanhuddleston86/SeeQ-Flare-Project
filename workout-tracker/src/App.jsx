import { useCallback, useEffect, useState } from 'react'
import NamePicker from './components/NamePicker.jsx'
import LogForm from './components/LogForm.jsx'
import WeeklyView from './components/WeeklyView.jsx'
import Leaderboard from './components/Leaderboard.jsx'
import { fetchLogs, insertLog, isConfigured } from './lib/supabase.js'
import { currentStreak } from './lib/stats.js'
import { startOfWeek, todayISO } from './lib/dates.js'

export default function App() {
  // Identity lives in component state only — reload and you pick again. With
  // three people on a shared program that is the whole auth story.
  const [name, setName] = useState(null)
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(isConfigured)
  const [loadError, setLoadError] = useState('')

  const load = useCallback(async () => {
    if (!isConfigured) return
    setLoading(true)
    setLoadError('')
    try {
      setLogs(await fetchLogs())
    } catch (err) {
      setLoadError(err.message || 'Could not load the logs.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function handleSubmit(entry) {
    const saved = await insertLog(entry)
    // Splice the new row in locally so the board updates the moment it saves,
    // without a second round trip.
    setLogs((previous) => [saved, ...previous])
  }

  if (!isConfigured) {
    return (
      <main className="app">
        <div className="card setup">
          <h2 className="card-title">Setup needed</h2>
          <p>
            Set <code>VITE_SUPABASE_URL</code> and <code>VITE_SUPABASE_ANON_KEY</code>, then rebuild.
            Locally that means a <code>.env.local</code> file; for the deployed site they are repository
            secrets read by the GitHub Actions workflow.
          </p>
          <p>Run <code>supabase/schema.sql</code> in the Supabase SQL editor first.</p>
        </div>
      </main>
    )
  }

  if (!name) {
    return (
      <main className="app">
        <NamePicker onPick={setName} />
      </main>
    )
  }

  const weekStart = startOfWeek(todayISO())
  const streak = currentStreak(logs, name)

  return (
    <main className="app">
      <header className="app-head">
        <div>
          <p className="hello">Hey {name}</p>
          <p className="hello-sub">
            {streak > 0 ? `${streak} week${streak === 1 ? '' : 's'} on target 🔥` : 'Get this week on the board'}
          </p>
        </div>
        <button type="button" className="link-button" onClick={() => setName(null)}>
          Not you?
        </button>
      </header>

      <LogForm name={name} onSubmit={handleSubmit} />

      {loadError && (
        <div className="card error-card">
          <p>{loadError}</p>
          <button type="button" className="link-button" onClick={load}>
            Try again
          </button>
        </div>
      )}

      {loading ? (
        <p className="loading">Loading the board…</p>
      ) : (
        <>
          <WeeklyView logs={logs} weekStart={weekStart} currentName={name} />
          <Leaderboard logs={logs} currentName={name} />
        </>
      )}

      <footer className="app-foot">
        <button type="button" className="link-button" onClick={load} disabled={loading}>
          Refresh
        </button>
      </footer>
    </main>
  )
}
