# Workout Accountability Tracker

A single-page app for Ryan, Clay, and John to log workouts and see each other's
consistency. No signup, no passwords — you pick your name on load.

- **Frontend:** React + Vite, built to static files and served from GitHub Pages
- **Data:** Supabase Postgres, called straight from the browser with the anon key
- **Deploy:** GitHub Actions on push to `main`

## Features

| Feature | What it does |
| --- | --- |
| Name picker | Three buttons on load. Identity is component state, cleared on reload. |
| Log form | Date (defaults to today), gym/home toggle, day 1–4 buttons, optional note, one submit. |
| Weekly view | Monday–Sunday grid per person, marks for logged day slots against a 3-day target (4 if the optional day was used). |
| Streak | Consecutive weeks with at least 3 sessions, shown in the header and on the leaderboard. |
| Leaderboard | Total sessions since the first logged workout, highest first. |

## Setup

### 1. Supabase

This part needs your Supabase account, so it has to be done by hand once:

1. Sign in at [supabase.com](https://supabase.com) and **New project**. The free
   tier is enough — three people logging four sessions a week is a few hundred
   rows a year.
2. Name it whatever you like, set a database password (you won't need it for
   this app), and pick the region closest to you.
3. When it finishes provisioning, open **SQL Editor → New query**, paste all of
   [`supabase/schema.sql`](supabase/schema.sql), and run it. That creates
   `workout_logs`, its indexes, and the row-level security policies.
4. Copy the project URL and the `anon` `public` key from
   **Project Settings → API**.

Then confirm it all landed:

```bash
npm run verify
```

That checks the table exists, that the anon key can read, and — the one that
matters — that row-level security actually rejects a write under an unknown
name. It writes nothing that survives. Fix anything it flags before deploying.

The policies allow `select` and `insert` only when `user_name` is one of the
three known names, and there is no `update` or `delete` policy at all. The anon
key is embedded in the built JavaScript — that is expected, and RLS is what
keeps the table from being writable with arbitrary data. Correct a bad entry
from the Supabase dashboard, which uses the service role and bypasses RLS.

Copy the project URL and anon key from **Project Settings → API**.

### 2. Local development

```bash
cd workout-tracker
npm install
cp .env.example .env.local   # fill in the two values
npm run dev
```

`npm test` runs the week-math, streak, and leaderboard unit tests.
`npm run verify` checks the Supabase project and its policies.
`npm run build` produces `dist/`.

### 3. Deploying to GitHub Pages

1. **Settings → Pages → Build and deployment → Source:** GitHub Actions.
2. **Settings → Secrets and variables → Actions:** add `VITE_SUPABASE_URL` and
   `VITE_SUPABASE_ANON_KEY`.
3. Push to `main`. `.github/workflows/deploy-workout-tracker.yml` installs,
   tests, builds, and publishes.

The site lands at `https://<user>.github.io/SeeQ-Flare-Project/`. That repo-name
prefix is baked in as Vite's `base`; for a custom domain build with
`VITE_BASE=/` instead.

## Conventions worth knowing

- **Weeks run Monday to Sunday.** A Sunday session counts toward the week that
  is ending, not the one starting.
- **Dates are local-time `YYYY-MM-DD` strings** throughout. Handing those to
  `new Date()` would parse them as UTC and shift the day backwards in US time
  zones, so `src/lib/dates.js` parses them by hand.
- **The optional day raises that week's bar to four** rather than banking credit
  toward the next week. Streaks still only need three.
- **One log per person, per date, per day slot**, enforced by a unique index. A
  duplicate surfaces as "You already logged that day for this date."

## Layout

```
workout-tracker/
├── src/
│   ├── App.jsx                  # name gate, data loading, layout
│   ├── components/              # NamePicker, LogForm, WeeklyView, Leaderboard
│   └── lib/
│       ├── dates.js             # local-date and week math
│       ├── stats.js             # week board, streaks, leaderboard
│       └── supabase.js          # client, fetch, insert
├── scripts/verify-supabase.mjs  # non-destructive schema and RLS check
├── supabase/schema.sql          # table, indexes, RLS policies
└── test/stats.test.js           # unit tests for the math above
```

## Not built yet

Stretch items, deliberately left for after the core settles: a per-person
calendar heatmap, per-exercise weight/reps logging, and a Sunday digest.
