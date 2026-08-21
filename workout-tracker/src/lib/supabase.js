import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

// Missing credentials shouldn't blow up the bundle on load — the app renders a
// setup notice instead, which is friendlier than a blank white screen.
export const isConfigured = Boolean(url && anonKey)

export const supabase = isConfigured
  ? createClient(url, anonKey, { auth: { persistSession: false } })
  : null

export async function fetchLogs() {
  if (!supabase) return []
  const { data, error } = await supabase
    .from('workout_logs')
    .select('id, user_name, log_date, track, day, note, created_at')
    .order('log_date', { ascending: false })
  if (error) throw error
  return data ?? []
}

export async function insertLog(entry) {
  if (!supabase) throw new Error('Supabase is not configured.')
  const { data, error } = await supabase.from('workout_logs').insert(entry).select().single()
  if (error) {
    // 23505 is the unique index on (user_name, log_date, day).
    if (error.code === '23505') throw new Error('You already logged that day for this date.')
    throw error
  }
  return data
}
