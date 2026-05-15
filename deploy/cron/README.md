# LOCALMEM retention cron

Cron drives the daily retention sweep by `curl`-ing the running dashboard
server. This is the only safe way to trigger consolidation/archive while
the server is up — Qdrant local doesn't allow concurrent writers across
processes, so spawning a separate `localmem prune --apply` would corrupt
state.

## macOS (launchd)

1. Stash your dashboard API key into the user-scoped launchd environment:

   ```bash
   launchctl setenv LOCALMEM_API_KEY "<your-key>"
   ```

2. Copy the plist into `~/Library/LaunchAgents` and load it:

   ```bash
   cp com.localmem.prune.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.localmem.prune.plist
   ```

3. Verify it's queued:

   ```bash
   launchctl list | grep localmem
   ```

To unload: `launchctl unload ~/Library/LaunchAgents/com.localmem.prune.plist`.

## Linux (systemd)

1. Drop the API key into `/etc/localmem/prune.env`:

   ```ini
   LOCALMEM_API_KEY=<your-key>
   ```

   `chmod 600 /etc/localmem/prune.env` — readable only by the user
   running the timer.

2. Install the unit + timer (system-wide install shown; for user units use
   `~/.config/systemd/user/`):

   ```bash
   sudo cp localmem-prune.service /etc/systemd/system/
   sudo cp localmem-prune.timer   /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now localmem-prune.timer
   ```

3. Inspect:

   ```bash
   systemctl list-timers localmem-prune.timer
   journalctl -u localmem-prune.service
   ```

## Auth

Both jobs hit `POST /api/prune/run` and `POST /api/archive/run` with a
bearer token. If `dashboard.auth_enabled` is `false` (default for local
installs), the token is still accepted but not required — the curl calls
above include it for forward compatibility.

## What runs

Each invocation drains the worker queue:
1. Reconcile any orphan source points (left over from prior partial runs).
2. Consolidate stale low-importance groups in each wing.
3. Reconcile any archive duplicates.
4. Archive entries past `max_age_days` for each wing (skipped for `shared`
   when `max_age_days: null`).

Failures are logged but never crash the daemon — the next tick retries.
