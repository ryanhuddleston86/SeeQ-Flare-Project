import { startOfWeek, addDays, todayISO } from './dates.js'

export const USERS = ['Ryan', 'Clay', 'John']
export const DAYS = ['Day 1', 'Day 2', 'Day 3', 'Optional Day 4']
export const TRACKS = ['gym', 'home']

// Three sessions a week is the program. A logged Optional Day 4 raises that
// person's bar for the week to four rather than banking credit toward the next.
export const WEEKLY_TARGET = 3
export const OPTIONAL_DAY = 'Optional Day 4'

function countsByWeek(logs, name) {
  const counts = new Map()
  for (const log of logs) {
    if (log.user_name !== name) continue
    const key = startOfWeek(log.log_date)
    counts.set(key, (counts.get(key) || 0) + 1)
  }
  return counts
}

// One row per name for the given week: which day slots are filled, how many
// sessions landed, and the target that week is judged against.
export function weekBoard(logs, weekStartISO) {
  const weekEnd = addDays(weekStartISO, 6)
  return USERS.map((name) => {
    const entries = logs.filter(
      (log) => log.user_name === name && log.log_date >= weekStartISO && log.log_date <= weekEnd,
    )
    const byDay = new Map()
    for (const entry of entries) {
      // The same day slot can be logged twice in a week on different dates.
      // Show the most recent, rather than whichever row happened to arrive first.
      const existing = byDay.get(entry.day)
      if (!existing || entry.log_date > existing.log_date) byDay.set(entry.day, entry)
    }
    const usedOptional = byDay.has(OPTIONAL_DAY)
    return {
      name,
      byDay,
      count: entries.length,
      target: usedOptional ? WEEKLY_TARGET + 1 : WEEKLY_TARGET,
      hitTarget: entries.length >= WEEKLY_TARGET,
    }
  })
}

// Consecutive weeks with at least WEEKLY_TARGET sessions. The current week only
// counts once it has already been hit — an in-progress Monday shouldn't read as
// a broken streak.
export function currentStreak(logs, name, today = todayISO()) {
  const counts = countsByWeek(logs, name)
  const thisWeek = startOfWeek(today)
  let week = (counts.get(thisWeek) || 0) >= WEEKLY_TARGET ? thisWeek : addDays(thisWeek, -7)
  let streak = 0
  while ((counts.get(week) || 0) >= WEEKLY_TARGET) {
    streak += 1
    week = addDays(week, -7)
  }
  return streak
}

// Total sessions since the program started, highest first. Ties break on name
// so the order stays stable between renders.
export function leaderboard(logs, today = todayISO()) {
  return USERS.map((name) => ({
    name,
    total: logs.filter((log) => log.user_name === name).length,
    streak: currentStreak(logs, name, today),
  })).sort((a, b) => b.total - a.total || a.name.localeCompare(b.name))
}

export function programStart(logs) {
  if (logs.length === 0) return null
  return logs.reduce((earliest, log) => (log.log_date < earliest ? log.log_date : earliest), logs[0].log_date)
}
