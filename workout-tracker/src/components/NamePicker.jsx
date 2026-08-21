import { USERS } from '../lib/stats.js'

// Identity is a name held in component state, nothing more. There are three
// people and no reason for anyone to impersonate anyone else.
export default function NamePicker({ onPick }) {
  return (
    <div className="picker">
      <h1 className="picker-title">Who's lifting?</h1>
      <p className="picker-sub">Pick your name to log and see the board.</p>
      <div className="picker-names">
        {USERS.map((name) => (
          <button key={name} type="button" className="picker-name" onClick={() => onPick(name)}>
            {name}
          </button>
        ))}
      </div>
    </div>
  )
}
