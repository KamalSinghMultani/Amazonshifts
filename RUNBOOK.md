# Amazon Shift Watcher — Command Runbook

Everything runs from the project folder:

    cd A:\PersonalProjects\Project2

PowerShell needs `.\` in front of scripts in the current folder (`.\run_watcher.bat`,
not `run_watcher.bat`).

---

## 1. One-time setup

Run these once. You do NOT repeat them when you close VS Code or reboot.

| When | Command |
|---|---|
| Log in to Amazon (once, and again only when the session expires) | `python save_session.py` |
| Install autostart so it runs at every Windows logon | `powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1` |
| Start it now, without logging out first | `Start-ScheduledTask -TaskName AmazonShiftWatcher` |
| Stop the machine sleeping (a sleeping PC catches nothing) | `powercfg /change standby-timeout-ac 0`<br>`powercfg /change hibernate-timeout-ac 0` |

After this the watcher starts automatically at logon. Nothing to run again.

---

## 2. Everyday use

| When | Command |
|---|---|
| Check it is alive and healthy | `python watcher.py --doctor` *(stop the task first — see below)* |
| Watch it work, live | `Get-Content logs\watcher.log -Tail 20 -Wait` |
| Is the scheduled task running? | `Get-ScheduledTask -TaskName AmazonShiftWatcher \| Get-ScheduledTaskInfo` |
| Pause it | `Stop-ScheduledTask -TaskName AmazonShiftWatcher` |
| Resume it | `Start-ScheduledTask -TaskName AmazonShiftWatcher` |
| When do shifts actually drop? (after a few days of data) | `python watcher.py --drop-report` |

### The one gotcha

Only ONE process can use the browser profile at a time. Before `--doctor` or
`save_session.py`, stop the watcher:

    Stop-ScheduledTask -TaskName AmazonShiftWatcher
    python watcher.py --doctor
    Start-ScheduledTask -TaskName AmazonShiftWatcher

Skipping this makes it hang on a profile lock and look broken when it is not.

---

## 3. Running it by hand instead of as a task

| When | Command |
|---|---|
| Run in a terminal, auto-restarting on crash (leave window open) | `.\run_watcher.bat` |
| One single poll, then exit | `python watcher.py --once` |
| Run without holding anything, alerts only | `python watcher.py --once` after setting `dry_run: true` in config.yaml |

---

## 4. When the session expires

You will be told: the watcher checks every 10 minutes and Telegram says
**"Amazon session expired"**, or `--doctor` reports `signed OUT`.

Three things cause it, in order of likelihood:

1. The session aged out on its own. How long that takes is unmeasured — the
   clean test is to leave one watcher running overnight and check in the
   morning
2. Two watchers were running off the same saved session, rotating each other's
   tokens
3. You ticked nothing at login. If the login page offers **"Keep me signed
   in"**, use it

Signing in on a second browser is NOT a cause — that was tested and the
session survived it.

Then:

    Stop-ScheduledTask -TaskName AmazonShiftWatcher
    python save_session.py
    Start-ScheduledTask -TaskName AmazonShiftWatcher

---

## 5. Testing (US site — never for real shifts)

The US site always has jobs, so it is where the code gets exercised. It has its
own profile, state and log.

**Detection needs no login**, so testing here is safe as shipped: it runs
`dry_run: true` and never signs in. Tested 2026-08-17 — a second browser login
on the same account did NOT sign the watcher out.

The one thing to avoid is two watchers running off the SAME saved session
(`auth_state.json` copied into a second config). Sessions rotate their tokens
and two clients on one chain can knock each other out. If you ever do sign in
for a US hold test, check Canada afterwards:

    python watcher.py --doctor        # expect: hiring portal login — signed in
    python save_session.py            # if it says signed OUT

| When | Command |
|---|---|
| Log in to the US site once | `python save_session.py --config config.us.yaml` |
| Check that environment | `python watcher.py --doctor --config config.us.yaml` |
| One test poll (alerts only, safe) | `python watcher.py --once --config config.us.yaml` |
| Run the full test suite (no browser or network needed) | `python -m pytest -q` |

---

## 6. What it does when a shift appears

1. Detects it within ~5 seconds (3 when hot); each poll takes ~150 ms
2. Telegram alert immediately
3. Opens the job, Select schedule, Apply, Next, **Create Application**
4. Telegram: **SPOT HELD** with the countdown, a screenshot, and a link
5. You have ~3 hours to finish the 7 application steps yourself

It holds ONE shift per poll — the best by your priorities: Brampton, then
Mississauga, then Toronto, and Fulfillment ahead of Delivery.

---

## 7. Changing what it looks for

All in `config.yaml`:

| Setting | Meaning |
|---|---|
| `filters.include_locations` | Cities it will consider |
| `filters.include_titles` | Job titles it will consider |
| `priority.locations` | Preference order when several match |
| `priority.demote_titles` | Roles pushed to the bottom (currently `delivery`) |
| `dry_run` | `true` = alert only, `false` = hold it |
| `hold.stop_before_submit` | `false` = press Create Application, `true` = stop short |

Restart the watcher after editing.
