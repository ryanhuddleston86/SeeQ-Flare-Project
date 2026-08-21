import test from 'node:test'
import assert from 'node:assert/strict'
import { startOfWeek, addDays, formatWeekRange } from '../src/lib/dates.js'
import { weekBoard, currentStreak, leaderboard, programStart } from '../src/lib/stats.js'

const log = (user_name, log_date, day, track = 'gym') => ({ user_name, log_date, day, track, note: null })

// Weeks are Monday-anchored; a Sunday belongs to the week that just ended.
test('startOfWeek anchors on Monday', () => {
  assert.equal(startOfWeek('2026-08-19'), '2026-08-17') // Wednesday
  assert.equal(startOfWeek('2026-08-17'), '2026-08-17') // Monday itself
  assert.equal(startOfWeek('2026-08-23'), '2026-08-17') // Sunday
})

test('date math does not drift across a month boundary', () => {
  assert.equal(addDays('2026-08-31', 1), '2026-09-01')
  assert.equal(addDays('2026-09-01', -1), '2026-08-31')
  assert.equal(formatWeekRange('2026-08-31'), 'Aug 31 – Sep 6')
})

test('weekBoard raises the target when the optional day is used', () => {
  const logs = [
    log('Ryan', '2026-08-17', 'Day 1'),
    log('Ryan', '2026-08-19', 'Day 2'),
    log('Ryan', '2026-08-21', 'Day 3'),
    log('Ryan', '2026-08-22', 'Optional Day 4'),
    log('Clay', '2026-08-18', 'Day 1', 'home'),
  ]
  const [ryan, clay, john] = weekBoard(logs, '2026-08-17')
  assert.equal(ryan.count, 4)
  assert.equal(ryan.target, 4)
  assert.equal(ryan.hitTarget, true)
  assert.equal(clay.count, 1)
  assert.equal(clay.target, 3)
  assert.equal(clay.hitTarget, false)
  assert.equal(john.count, 0)
})

test('weekBoard excludes sessions outside the week', () => {
  const logs = [log('Ryan', '2026-08-16', 'Day 1'), log('Ryan', '2026-08-24', 'Day 1')]
  assert.equal(weekBoard(logs, '2026-08-17')[0].count, 0)
})

test('currentStreak counts consecutive weeks at three or more', () => {
  const logs = [
    ...['2026-08-17', '2026-08-19', '2026-08-21'].map((d) => log('Ryan', d, 'Day 1')),
    ...['2026-08-10', '2026-08-12', '2026-08-14'].map((d) => log('Ryan', d, 'Day 1')),
    ...['2026-08-03', '2026-08-05'].map((d) => log('Ryan', d, 'Day 1')), // short week breaks it
  ]
  assert.equal(currentStreak(logs, 'Ryan', '2026-08-21'), 2)
})

// An in-progress week that hasn't reached three yet must not zero out a streak
// built over previous weeks.
test('currentStreak ignores an unfinished current week', () => {
  const logs = [
    log('Ryan', '2026-08-17', 'Day 1'),
    ...['2026-08-10', '2026-08-12', '2026-08-14'].map((d) => log('Ryan', d, 'Day 1')),
  ]
  assert.equal(currentStreak(logs, 'Ryan', '2026-08-18'), 1)
})

test('currentStreak is zero with no qualifying weeks', () => {
  assert.equal(currentStreak([log('Ryan', '2026-08-17', 'Day 1')], 'Ryan', '2026-08-21'), 0)
  assert.equal(currentStreak([], 'John', '2026-08-21'), 0)
})

test('leaderboard sorts by total sessions and lists every user', () => {
  const logs = [
    log('Clay', '2026-08-17', 'Day 1'),
    log('Clay', '2026-08-18', 'Day 2'),
    log('Ryan', '2026-08-17', 'Day 1'),
  ]
  const board = leaderboard(logs, '2026-08-21')
  assert.deepEqual(
    board.map((row) => [row.name, row.total]),
    [['Clay', 2], ['Ryan', 1], ['John', 0]],
  )
})

test('programStart is the earliest logged date', () => {
  const logs = [log('Ryan', '2026-08-17', 'Day 1'), log('Clay', '2026-07-02', 'Day 1')]
  assert.equal(programStart(logs), '2026-07-02')
  assert.equal(programStart([]), null)
})

// The slot shown must not depend on the order rows come back from the database.
test('weekBoard shows the most recent session for a repeated day slot', () => {
  const early = { ...log('Ryan', '2026-08-17', 'Day 1'), note: 'early' }
  const late = { ...log('Ryan', '2026-08-20', 'Day 1'), note: 'late' }
  for (const order of [[early, late], [late, early]]) {
    const row = weekBoard(order, '2026-08-17')[0]
    assert.equal(row.byDay.get('Day 1').note, 'late')
    assert.equal(row.count, 2)
  }
})
