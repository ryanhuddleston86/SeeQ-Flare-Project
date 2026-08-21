import { leaderboard, programStart } from '../lib/stats.js'
import { formatShort } from '../lib/dates.js'

const MEDALS = ['🥇', '🥈', '🥉']

// Total sessions since the first logged workout, plus each person's run of
// weeks that hit the target.
export default function Leaderboard({ logs, currentName }) {
  const rows = leaderboard(logs)
  const start = programStart(logs)

  return (
    <section className="card">
      <div className="card-head">
        <h2 className="card-title">Leaderboard</h2>
        <span className="card-meta">{start ? `since ${formatShort(start)}` : 'no sessions yet'}</span>
      </div>

      <ol className="board">
        {rows.map((row, index) => (
          <li key={row.name} className={`board-row ${row.name === currentName ? 'is-you' : ''}`}>
            <span className="board-rank">{MEDALS[index] ?? index + 1}</span>
            <span className="board-name">{row.name}</span>
            <span className="board-streak" title="Consecutive weeks with 3+ sessions">
              {row.streak > 0 ? `🔥 ${row.streak}w` : '—'}
            </span>
            <span className="board-total">
              {row.total}
              <span className="board-total-unit">{row.total === 1 ? 'session' : 'sessions'}</span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  )
}
