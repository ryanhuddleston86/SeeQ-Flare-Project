// All dates are handled as local-time "YYYY-MM-DD" strings. Going through
// Date's UTC parsing (new Date("2026-08-21")) would shift the day backwards for
// anyone west of UTC, which is exactly the population logging these workouts.

export function toISODate(date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function parseISODate(iso) {
  const [year, month, day] = iso.split('-').map(Number)
  return new Date(year, month - 1, day)
}

export function todayISO() {
  return toISODate(new Date())
}

export function addDays(iso, amount) {
  const date = parseISODate(iso)
  date.setDate(date.getDate() + amount)
  return toISODate(date)
}

// Weeks run Monday through Sunday. The returned Monday doubles as the week's
// identity key everywhere else in the app.
export function startOfWeek(iso) {
  const date = parseISODate(iso)
  const offset = (date.getDay() + 6) % 7
  date.setDate(date.getDate() - offset)
  return toISODate(date)
}

export function weekDays(weekStartISO) {
  return Array.from({ length: 7 }, (_, i) => addDays(weekStartISO, i))
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

export function formatShort(iso) {
  const date = parseISODate(iso)
  return `${MONTHS[date.getMonth()]} ${date.getDate()}`
}

export function formatWeekday(iso) {
  return WEEKDAYS[parseISODate(iso).getDay()]
}

export function formatWeekRange(weekStartISO) {
  return `${formatShort(weekStartISO)} – ${formatShort(addDays(weekStartISO, 6))}`
}
