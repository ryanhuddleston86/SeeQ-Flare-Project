import { useState } from 'react'
import { DAYS, TRACKS } from '../lib/stats.js'
import { todayISO } from '../lib/dates.js'

// Optimised for logging from a phone between sets: the date is prefilled, the
// track remembers nothing surprising, and one tap per field gets you to submit.
export default function LogForm({ name, onSubmit }) {
  const [logDate, setLogDate] = useState(todayISO)
  const [track, setTrack] = useState('gym')
  const [day, setDay] = useState('Day 1')
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    if (saving) return
    setSaving(true)
    setError('')
    setSaved(false)
    try {
      await onSubmit({
        user_name: name,
        log_date: logDate,
        track,
        day,
        note: note.trim() || null,
      })
      setNote('')
      setSaved(true)
      // Clear the confirmation on its own so the form is ready for next time.
      setTimeout(() => setSaved(false), 2500)
    } catch (err) {
      setError(err.message || 'Could not save that entry.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="card log-form" onSubmit={handleSubmit}>
      <h2 className="card-title">Log a session</h2>

      <label className="field">
        <span className="field-label">Date</span>
        <input
          type="date"
          className="date-input"
          value={logDate}
          max={todayISO()}
          onChange={(event) => setLogDate(event.target.value)}
          required
        />
      </label>

      <div className="field">
        <span className="field-label">Track</span>
        <div className="segmented">
          {TRACKS.map((option) => (
            <button
              key={option}
              type="button"
              className={`segment ${track === option ? 'is-active' : ''}`}
              aria-pressed={track === option}
              onClick={() => setTrack(option)}
            >
              {option === 'gym' ? 'Gym' : 'Home'}
            </button>
          ))}
        </div>
      </div>

      <div className="field">
        <span className="field-label">Day</span>
        <div className="day-grid">
          {DAYS.map((option) => (
            <button
              key={option}
              type="button"
              className={`day-button ${day === option ? 'is-active' : ''}`}
              aria-pressed={day === option}
              onClick={() => setDay(option)}
            >
              {option === 'Optional Day 4' ? 'Day 4' : option}
              {option === 'Optional Day 4' && <span className="day-tag">optional</span>}
            </button>
          ))}
        </div>
      </div>

      <label className="field">
        <span className="field-label">Note <span className="field-hint">optional</span></span>
        <input
          type="text"
          className="text-input"
          value={note}
          maxLength={140}
          placeholder="Squats felt heavy"
          onChange={(event) => setNote(event.target.value)}
        />
      </label>

      {error && <p className="form-error">{error}</p>}

      <button type="submit" className="submit" disabled={saving}>
        {saving ? 'Saving…' : saved ? 'Logged ✓' : 'Log it'}
      </button>
    </form>
  )
}
