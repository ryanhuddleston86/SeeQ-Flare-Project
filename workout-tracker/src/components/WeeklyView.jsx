import { DAYS, OPTIONAL_DAY, weekBoard } from '../lib/stats.js'
import { formatWeekRange } from '../lib/dates.js'

// This week at a glance: one row per person, one mark per day slot.
export default function WeeklyView({ logs, weekStart, currentName }) {
  const rows = weekBoard(logs, weekStart)

  return (
    <section className="card">
      <div className="card-head">
        <h2 className="card-title">This week</h2>
        <span className="card-meta">{formatWeekRange(weekStart)}</span>
      </div>

      <ul className="week-list">
        {rows.map((row) => (
          <li key={row.name} className={`week-row ${row.name === currentName ? 'is-you' : ''}`}>
            <div className="week-row-head">
              <span className="week-name">{row.name}</span>
              <span className={`week-count ${row.hitTarget ? 'is-hit' : ''}`}>
                {row.count}/{row.target}
              </span>
            </div>
            <div className="week-marks">
              {DAYS.map((day) => {
                const entry = row.byDay.get(day)
                const optional = day === OPTIONAL_DAY
                const label = optional ? '4' : day.replace('Day ', '')
                const title = entry
                  ? `${day} — ${entry.track}${entry.note ? ` — ${entry.note}` : ''}`
                  : `${day} — not logged`
                return (
                  <span
                    key={day}
                    className={`mark ${entry ? 'is-done' : ''} ${optional ? 'is-optional' : ''}`}
                    title={title}
                  >
                    {entry ? '✓' : label}
                  </span>
                )
              })}
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
