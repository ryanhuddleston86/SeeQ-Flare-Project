#!/usr/bin/env node
// Checks that a Supabase project is wired up the way the app expects:
// the table exists, reads work, and row-level security actually blocks the
// things it is supposed to block.
//
//   npm run verify
//
// Every check is non-destructive — nothing here writes a row that survives.

import { readFileSync } from 'node:fs'

function loadEnv() {
  const env = { ...process.env }
  for (const file of ['.env.local', '.env']) {
    try {
      for (const line of readFileSync(new URL(`../${file}`, import.meta.url), 'utf8').split('\n')) {
        const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/)
        // Real environment variables win over file values.
        if (match && !env[match[1]]) env[match[1]] = match[2].replace(/^["']|["']$/g, '')
      }
    } catch {
      // File is optional.
    }
  }
  return env
}

const env = loadEnv()
const url = (env.VITE_SUPABASE_URL || '').replace(/\/$/, '')
const key = env.VITE_SUPABASE_ANON_KEY || ''

if (!url || !key) {
  console.error('Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY.')
  console.error('Copy .env.example to .env.local and fill in both values, then rerun.')
  process.exit(1)
}

const endpoint = `${url}/rest/v1/workout_logs`
const headers = { apikey: key, Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' }

const results = []
const record = (ok, name, detail) => {
  results.push({ ok, name, detail })
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${name}${detail ? ` — ${detail}` : ''}`)
}

// PostgREST reports errors as a JSON object carrying a `code`/`message`. A 403
// from a proxy or network allowlist arrives as a bare string or HTML, and must
// not be mistaken for a policy doing its job.
function isPostgrestError(payload) {
  return Boolean(payload) && typeof payload === 'object' && !Array.isArray(payload) &&
    (typeof payload.code === 'string' || typeof payload.message === 'string')
}

async function request(method, path, body) {
  const response = await fetch(`${endpoint}${path}`, {
    method,
    headers: { ...headers, ...(method === 'POST' ? { Prefer: 'return=representation' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  })
  const text = await response.text()
  let payload = null
  try {
    payload = text ? JSON.parse(text) : null
  } catch {
    payload = text
  }
  return { status: response.status, payload }
}

console.log(`\nChecking ${url}\n`)

// 1. The table exists and the select policy lets the anon key read.
const read = await request('GET', '?select=id&limit=1')
if (read.status === 200) {
  record(true, 'table exists and is readable')
} else if (!isPostgrestError(read.payload)) {
  // Nothing past this point can be trusted if the host is unreachable.
  console.error(`\n Cannot reach ${url} — HTTP ${read.status}: ${JSON.stringify(read.payload)}`)
  console.error(' This is a network or proxy problem, not a Supabase one. Check the URL and your connection.\n')
  process.exit(1)
} else if (read.status === 404) {
  record(false, 'table exists and is readable', 'workout_logs not found — run supabase/schema.sql')
} else if (read.status === 401) {
  record(false, 'table exists and is readable', 'anon key rejected — recheck the key')
} else {
  record(false, 'table exists and is readable', `HTTP ${read.status}: ${JSON.stringify(read.payload)}`)
}

// 2. RLS must reject a name outside the three known users. A 201 here means the
//    public anon key can write arbitrary rows, which is the whole thing the
//    policies exist to prevent.
const intruder = await request('POST', '', {
  user_name: 'NotARealUser',
  log_date: '2020-01-01',
  track: 'gym',
  day: 'Day 1',
})
if ((intruder.status === 401 || intruder.status === 403) && isPostgrestError(intruder.payload)) {
  record(true, 'RLS blocks unknown user names', `rejected with ${intruder.payload.code || 'no code'}`)
} else if (intruder.status === 401 || intruder.status === 403) {
  record(false, 'RLS blocks unknown user names', `rejected by something other than PostgREST: ${JSON.stringify(intruder.payload)}`)
} else if (intruder.status === 201) {
  record(false, 'RLS blocks unknown user names', 'INSERT SUCCEEDED — the table is writable by anyone. Rerun supabase/schema.sql')
} else if (intruder.status === 400) {
  // The CHECK constraint caught it before RLS did. Still rejected, but the
  // policy itself went untested.
  record(true, 'RLS blocks unknown user names', 'rejected by CHECK constraint')
} else {
  record(false, 'RLS blocks unknown user names', `unexpected HTTP ${intruder.status}: ${JSON.stringify(intruder.payload)}`)
}

// 3. A known name with a bad track must fail the CHECK constraint. This proves
//    the constraints are in place without leaving a row behind.
const badTrack = await request('POST', '', {
  user_name: 'Ryan',
  log_date: '2020-01-01',
  track: 'not-a-track',
  day: 'Day 1',
})
if (badTrack.status === 400 && isPostgrestError(badTrack.payload)) {
  record(true, 'CHECK constraints reject bad track values')
} else if (badTrack.status === 201) {
  record(false, 'CHECK constraints reject bad track values', 'a row was created and must be deleted from the dashboard')
} else {
  record(false, 'CHECK constraints reject bad track values', `unexpected HTTP ${badTrack.status}: ${JSON.stringify(badTrack.payload)}`)
}

// 4. Informational: how much is already logged.
if (read.status === 200) {
  const count = await fetch(`${endpoint}?select=id`, {
    headers: { ...headers, Prefer: 'count=exact', Range: '0-0' },
  })
  const total = (count.headers.get('content-range') || '').split('/')[1]
  console.log(`\n${total && total !== '*' ? total : 0} session(s) logged so far.`)
}

const failed = results.filter((result) => !result.ok)
if (failed.length > 0) {
  console.error(`\n${failed.length} check(s) failed. See supabase/schema.sql and the README.\n`)
  process.exit(1)
}
console.log('\nAll checks passed. Run `npm run dev` and log a session.\n')
