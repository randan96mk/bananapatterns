# Banana Patterns in the cloud (Oracle Cloud Always Free + Tailscale)

The whole app (dashboard, scheduler, scans, Telegram alerts and bot) runs 24×7 on a free Oracle Cloud server,
so nothing depends on the Mac being awake. Only your own devices can open it: the dashboard is published on
your private Tailscale network with HTTPS, never on the open internet.

Use this file in three ways:
- **First time**: follow [Part 1](#part-1-first-time-setup) top to bottom (about 1–2 hours, mostly waiting).
- **Day to day**: [Daily use](#part-2-daily-use), [Updating after a PR](#part-3-code-changes-and-updates),
  [Backups](#part-4-backups-and-restore).
- **When something is wrong**: [Troubleshooting](#part-6-troubleshooting) and the
  [command cheat sheet](#part-7-command-cheat-sheet).

Placeholders: `<public-ip>` is the server's public IPv4 (Oracle instance page), `<tailnet>` is your tailnet
name (Tailscale admin → DNS, e.g. `tail1234ab.ts.net`), so the app lives at `https://banana.<tailnet>`.

---

## Contents
- [How it fits together](#how-it-fits-together)
- [Part 1: First-time setup](#part-1-first-time-setup)
  - [1.1 Oracle account, budget alert, Pay As You Go](#11-oracle-account-budget-alert-pay-as-you-go)
  - [1.2 Network (VCN wizard)](#12-network-vcn-wizard)
  - [1.3 SSH key on the Mac](#13-ssh-key-on-the-mac)
  - [1.4 Create the server](#14-create-the-server)
  - [1.5 Prepare the server (server-setup.sh)](#15-prepare-the-server-server-setupsh)
  - [1.6 Tailscale admin settings](#16-tailscale-admin-settings)
  - [1.7 Code, first start, private HTTPS](#17-code-first-start-private-https)
  - [1.8 Move your data from the Mac (switch-over)](#18-move-your-data-from-the-mac-switch-over)
  - [1.9 Phones and other computers](#19-phones-and-other-computers)
  - [1.10 Nightly backups](#110-nightly-backups)
- [Part 2: Daily use](#part-2-daily-use)
- [Part 3: Code changes and updates](#part-3-code-changes-and-updates)
- [Part 4: Backups and restore](#part-4-backups-and-restore)
- [Part 5: Maintenance](#part-5-maintenance)
- [Part 6: Troubleshooting](#part-6-troubleshooting)
- [Part 7: Command cheat sheet](#part-7-command-cheat-sheet)

---

## How it fits together

```
 Mac / iPhone / Android                Oracle Cloud (Hyderabad), Always Free
 (Tailscale app, same account)         VM "banana": Ubuntu 24.04 ARM, A1.Flex 2 OCPU
            │                          ┌───────────────────────────────────────────────┐
            │  https://banana.<tailnet>│ tailscale serve (HTTPS, tailnet only)          │
            └──────── WireGuard ───────┼──► 127.0.0.1:8000                             │
                                       │      Docker: bananapatterns container         │
                                       │        FastAPI + dashboard + scheduler + bot  │
                                       │        data/ (SQLite) logs/ config/  .env     │
                                       │   cron 23:30: scripts/backup.sh → ~/banana-backups
                                       └───────┬───────────────┬───────────────┬───────┘
                                               │               │               │
                                        Kite Connect      NSE public data   Telegram
                                        (quotes, history) (results, filings, (alerts, bot,
                                                           deals, holidays)   login links)
```

- **Public internet → server**: only SSH (port 22, key login only). Port 8000 listens on `127.0.0.1` only;
  Tailscale needs no open port.
- **Cost**: ₹0 inside the Always Free limits (A1 up to 4 OCPU / 24 GB in total, 200 GB of disk). We use
  2 OCPU / 6–12 GB and a 50 GB disk.
- **Secrets** (`.env`: Kite key/secret, Telegram bot token, dashboard password) live only on the server (and in
  its backups), never in git.
- **One copy only**: the cloud copy and the Mac copy share one Kite API key (a new login logs the other out) and
  one Telegram bot (two copies send every alert twice). See [fallback to the Mac](#fallback-to-the-mac).

---

## Part 1: First-time setup

### 1.1 Oracle account, budget alert, Pay As You Go
1. Sign up at [oracle.com/cloud/free](https://www.oracle.com/cloud/free/). Pick the **home region** carefully
   (it can't be changed): we use **India South (Hyderabad)**. A card is needed for identity checks.
2. **Budget alert** (emails you if anything is ever charged): ☰ → Billing & Cost Management → **Budgets** →
   Create Budget: scope your root compartment, monthly, amount `100`; alert rule **Actual spend**,
   **Percentage of budget** `1` %, your email. Free usage never counts.
3. **Upgrade to Pay As You Go** (recommended): ☰ → Billing & Cost Management → Upgrade and Manage Payment.
   - Free-only accounts get the lowest priority for A1 servers ("Out of capacity") and idle free servers can be
     reclaimed; PAYG fixes both and still costs ₹0 inside the Always Free limits.
   - Oracle puts a **temporary hold** of about US$100 on the card (e.g. SGD 138 on an SGD card). It is an
     authorisation, not a charge: it shows as *Pending* and drops off after 3–7 business days (some banks: up to
     30). Check Billing → **Cost analysis** = 0 and no **Invoices**. If it is ever *Posted*, raise a billing
     support request.

### 1.2 Network (VCN wizard)
Use the **wizard**, not the plain "Create VCN" form (that one makes a network with no internet gateway and the
server's public IP toggle stays greyed out).

☰ → Networking → **Virtual cloud networks** → compartment = root → **Actions → Start VCN Wizard** →
**Create VCN with Internet Connectivity** → name `banana-vcn`, leave the defaults (VCN `10.0.0.0/16`, public
subnet `10.0.0.0/24`, private `10.0.1.0/24`, IPv6 off) → Next → **Create**. You get the VCN, both subnets,
an internet gateway, NAT and service gateways, and a security list that allows SSH (22) only.

### 1.3 SSH key on the Mac
```bash
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C "banana-oracle"   # Enter for the default file
pbcopy < ~/.ssh/id_ed25519.pub                                          # public half to the clipboard
```
Only the `.pub` file ever goes to Oracle. Never share `~/.ssh/id_ed25519`.

### 1.4 Create the server
☰ → Compute → Instances → **Create instance**:

| Section | Value |
|---|---|
| Name | `banana` |
| Image | Canonical **Ubuntu 24.04** (not Minimal); aarch64 is chosen with the shape |
| Shape | Ampere → **VM.Standard.A1.Flex**, **2 OCPU**, **12 GB** (6 GB also fine) |
| Networking | existing VCN `banana-vcn`, subnet **public subnet-banana-vcn**, **Automatically assign public IPv4: ON** |
| SSH keys | **Paste public key** (⌘V) |
| Boot volume | **Specify a custom size: 50 GB** |

The IPv6 warning ("subnet does not support IPv6") is expected; ignore it.

**"Out of capacity for shape VM.Standard.A1.Flex"** is common (Hyderabad has a single availability domain):
- On the Review page click **Save as stack** (name `banana-instance`) instead of Create. To retry later:
  search **Stacks** (or ☰ → Developer Services → Resource Manager → Stacks) → `banana-instance` → **Apply** →
  Apply. A failed job creates nothing and costs nothing.
- Try a smaller shape (1 OCPU / 6 GB), retry early morning or late night, and after the PAYG upgrade.

**After it is Running**:
- Instance page → **Shape configuration**: if **Burstable baseline** shows **50%**, change it to **100%**
  (Edit → Edit shape → baseline 100 % / non-burstable, and memory 12 GB while there; one reboot). The Always Free
  allowance is described for regular A1. If the change says "Out of capacity", keep the server and retry later.
- Note the **Public IP address**. Optional: Edit → rename to `banana`.

Test SSH from the Mac (first time answer `yes`):
```bash
ssh ubuntu@<public-ip>
```

### 1.5 Prepare the server (server-setup.sh)
The script does everything below in one go and is safe to run again; each step is skipped when already done.
```bash
# on the Mac, in the bananapatterns folder
scp scripts/server-setup.sh ubuntu@<public-ip>:
ssh ubuntu@<public-ip>
bash server-setup.sh
```
What it does:
1. Ubuntu updates, `git sqlite3 curl rsync`, time zone **Asia/Kolkata** (the scheduler runs on IST), hostname
   `banana`.
2. A 2 GB swap file (headroom for the image build).
3. Docker Engine + compose plugin from Docker's own repository; adds `ubuntu` to the `docker` group.
4. Tailscale: it prints a login link: open it in a browser and log in with **the same account you will use on
   every device** (Google, Microsoft, Apple or GitHub; Tailscale has no password of its own).
5. A GitHub **deploy key** (prints its public half; see 1.7).
6. Reachability check: NSE events API and bulk CSV should be **200**, Kite anything but `000`, Telegram 200/302.
   (NSE's *home page* answers 403 to scripts everywhere; the app ignores that and uses the APIs.)

Then **log out and back in** (`exit`, `ssh ubuntu@<public-ip>`) so `docker` works without `sudo`, and check:
`docker ps` (empty table), `tailscale ip -4` (a `100.x.y.z` address). From the Mac: `ping -c 3 banana`.

Ignore "New release '26.04' available": don't run `do-release-upgrade` (24.04 is supported until 2029).

### 1.6 Tailscale admin settings
[login.tailscale.com/admin](https://login.tailscale.com/admin):
1. **Machines** → `banana` → ⋯ → **Disable key expiry** (otherwise the server drops off after 180 days).
2. **DNS** → **MagicDNS: Enable** and **HTTPS Certificates: Enable** (the buttons then read "Disable…").
   Note the **tailnet name** shown there.
3. Mac: install Tailscale (App Store or tailscale.com/download), log in with the same account, and in its
   settings keep **Use Tailscale DNS settings** on.

### 1.7 Code, first start, private HTTPS
1. GitHub → `bananapatterns` → Settings → **Deploy keys** → Add deploy key: title `banana-oracle`, paste the
   server's `~/.ssh/id_ed25519.pub`, **leave "Allow write access" off**. The server can read this repository
   and nothing else.
2. On the server:
   ```bash
   ssh -o StrictHostKeyChecking=accept-new -T git@github.com     # "Hi randan96mk/bananapatterns! ..."
   git clone git@github.com:randan96mk/bananapatterns.git ~/bananapatterns
   cd ~/bananapatterns
   cp .env.example .env            # placeholder until the switch-over brings the real one
   ./docker.sh up                  # first build on ARM: 5-10 min, ends with "Waiting for the app.... ✓"
   sudo tailscale serve --bg 8000  # https://banana.<tailnet> -> 127.0.0.1:8000, tailnet only
   tailscale serve status
   ```
3. Open `https://banana.<tailnet>` on the Mac (the first visit can take ~30 s while the certificate is issued).
   With the placeholder `.env` you see an empty dashboard and "Kite: login needed": it can't log in, scan or
   send alerts, so it is safe next to the Mac copy.

`tailscale serve --bg` and the container (`restart: unless-stopped`, Docker enabled at boot) both come back on
their own after a reboot.

### 1.8 Move your data from the Mac (switch-over)
Do it after the evening scan or at a weekend. Takes ~15 minutes.

1. **Server**: stop the empty copy and set its files aside:
   ```bash
   cd ~/bananapatterns && ./docker.sh down
   mv data data.empty && mv .env .env.template-unused
   ```
2. **Mac**: stop the Mac copy and copy secrets, settings and the database (over Tailscale, not through git):
   ```bash
   cd ~/bananapatterns            # your Mac app folder
   ./docker.sh down               # or Ctrl+C in the ./start.sh window
   rsync -av --progress .env config data ubuntu@banana:~/bananapatterns/
   ```
3. **Server**: point the URLs at the cloud (values of secrets are never printed):
   ```bash
   cd ~/bananapatterns && chmod 600 .env
   URL=https://banana.<tailnet>
   sed -i "s|^KITE_REDIRECT_URL=.*|KITE_REDIRECT_URL=$URL/api/kite/callback|" .env
   grep -q '^PUBLIC_BASE_URL=' .env && sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$URL|" .env \
       || echo "PUBLIC_BASE_URL=$URL" >> .env
   awk -F= '/^[A-Z_]+=/{v=substr($0,index($0,"=")+1); print $1, (v==""?"(empty)":"(set)")}' .env
   ```
   Recommended: set `BP_DASHBOARD_PASSWORD` too (`nano .env`), even behind Tailscale.
4. **Kite developer console** ([developers.kite.trade](https://developers.kite.trade) → My apps → your app):
   Redirect URL = `https://banana.<tailnet>/api/kite/callback` → Save.
5. **Server**: `./docker.sh up`. Open `https://banana.<tailnet>` → **Kite login** → back on the dashboard with
   Kite connected and a live ticker; your screens, positions and watchlist are there. Alerts → send a test
   message to check Telegram.
6. **Mac**: don't start the Mac copy again (optionally untick Docker Desktop → Settings → General → *Start
   Docker Desktop when you sign in*). Remove the launchd agent if you used `scripts/macos/`.

### 1.9 Phones and other computers
Every device needs the Tailscale app logged in with **the same account**; nothing else is installed.

- **iPhone**: App Store → Tailscale → Log in (same account) → allow the VPN configuration → Connected (VPN badge).
  Safari → `https://banana.<tailnet>` → Share → **Add to Home Screen**. Optional: Tailscale → Settings →
  **VPN On Demand** on, so it connects by itself.
- **Android**: Play Store → Tailscale → same account → allow the VPN → Chrome → the URL → ⋮ → **Add to Home
  screen**.
- **Another computer**: install Tailscale, same account, open the URL.

Only Tailscale traffic to your own devices goes through the VPN; normal browsing is unaffected. Kite logins
and the Telegram login link work from the phone because the redirect URL is the tailnet address.

### 1.10 Nightly backups
```bash
cd ~/bananapatterns
scripts/backup.sh                 # first backup now: "OK ~/banana-backups/YYYY-MM-DD (..., NN tables, ...)"
scripts/backup.sh --install-cron  # every night at 23:30 IST
crontab -l                        # shows the line
```
Details in [Part 4](#part-4-backups-and-restore).

---

## Part 2: Daily use

What runs on its own (all IST, server clock): the evening scan and EOD alerts, the morning plan and opening
check, intraday heads-ups, NSE calendars / filings / deals refreshes, the position guard after the scan, and the
backup at 23:30. See the README's [Telegram alerts and automatic jobs](../README.md#telegram-alerts-and-automatic-jobs).

**Kite login once a day**: the token expires at 06:00 IST. Log in from the dashboard (Kite login button) or tap
the Telegram login link (or send `/login` to the bot) on any device with Tailscale on.

Quick health check from anywhere:
- Dashboard header / Data tab, the bot's `/status`.
- On the server: `cd ~/bananapatterns && ./docker.sh status` (container state + `/health`), `./docker.sh logs`.

---

## Part 3: Code changes and updates

### The normal cycle
1. A change is developed on a branch and opened as a **pull request** (each feature is planned and approved
   first, as in the README's Roadmap). Tests: `.venv/bin/python -m pytest -q` on a development machine.
2. You review and **merge** it on GitHub.
3. On the server:
   ```bash
   ssh ubuntu@banana
   cd ~/bananapatterns && ./docker.sh update      # git pull + rebuild + restart (~3-5 min on ARM)
   ```
4. Reload the dashboard with a hard refresh (⌘⇧R on the Mac; on the phone pull to refresh or reopen) so the
   browser loads the new dashboard code.
5. If something looks wrong: `./docker.sh logs`, and see [Troubleshooting](#part-6-troubleshooting).

`.env`, `data/` and `logs/` are ignored by git, so `git pull` never touches them. `config/` holds tracked
defaults (`nse_holidays.json`) that the app also refreshes; if `update` stops on it, see
[Troubleshooting](#part-6-troubleshooting). A new setting in `.env.example` is announced in the PR: add it to
the server's `.env` by hand.

### Roll back to an earlier version
```bash
cd ~/bananapatterns
git log --oneline -10                 # pick the last good commit
git checkout <commit>                 # "detached HEAD" is expected
./docker.sh rebuild                   # rebuild from the code as it is now (no pull)
# later, back to the latest:
git checkout main && ./docker.sh update
```
Database changes are normally additive (new tables / columns), so an older version usually runs on a newer
database. If it doesn't, restore the backup from the night before the update ([Part 4](#part-4-backups-and-restore)):
run `scripts/backup.sh` by hand before any big update, so that backup is fresh.
The cleaner long-term fix is a revert PR on GitHub, then `./docker.sh update`.

### Trying changes on the Mac while the cloud copy runs
Never let two copies run the jobs, send Telegram messages or log in to Kite at the same time.
- Use a copy of a backup database, not a live one: `scripts/pull-backups.sh`, then
  `gunzip -c ~/banana-backups-cloud/<day>/bananapatterns.db.gz > /tmp/bp-test.db`.
- Start the Mac copy with the scheduler off and that database:
  `BP_NO_SCHEDULER=1 BP_DB_PATH=/tmp/bp-test.db ./start.sh`.
- **Don't press Kite login on the Mac** (it would log the cloud copy out until you log in there again).
- For a full end-to-end check, deploy to the server instead (merge, `./docker.sh update`).

### Fallback to the Mac
If the server is down for a long time:
1. On the server (if reachable): `./docker.sh down`.
2. Get the latest data onto the Mac: `scripts/pull-backups.sh`, then restore it into the Mac folder
   (`gunzip -c ~/banana-backups-cloud/<day>/bananapatterns.db.gz > data/bananapatterns.db`, with the Mac copy
   stopped).
3. Kite console: Redirect URL back to `http://localhost:8000/api/kite/callback`; in the Mac `.env`,
   `KITE_REDIRECT_URL` the same and `PUBLIC_BASE_URL` empty.
4. Mac: `./docker.sh up` (or `./start.sh`), Kite login.

Moving back to the cloud is the [switch-over](#18-move-your-data-from-the-mac-switch-over) again.
Kite allows **one** redirect URL per app: whichever copy runs must match it.

### Rebuild the server from scratch
New VM ([1.4](#14-create-the-server)) → `server-setup.sh` ([1.5](#15-prepare-the-server-server-setupsh)) → add
its new deploy key and clone ([1.7](#17-code-first-start-private-https)) → copy `.env`, `config/` and the
database from a backup ([Part 4](#restore)) → `./docker.sh up` → `sudo tailscale serve --bg 8000`.
In the Tailscale admin, remove the old `banana` machine first, so the new one gets the same name (and URL).

---

## Part 4: Backups and restore

`scripts/backup.sh` (cron, 23:30 IST) writes `~/banana-backups/YYYY-MM-DD/`, outside the repository:

| File | What |
|---|---|
| `bananapatterns.db.gz` | the database, via SQLite's online `.backup` (consistent while the app runs), checked with `PRAGMA integrity_check` |
| `env` | a copy of `.env` (secrets: the folder is `700`, files `600`) |
| `config.tar.gz` | `config/` |

- Keeps the newest **14** days (`BP_BACKUP_KEEP`), about a few MB each.
- **Success is silent**; every run adds a line to `logs/backup.log`.
- **A failure** (database missing, copy corrupt, disk full, ...) is logged and sent to your **Telegram** chat
  (the bot token from `.env`; the chat from `TELEGRAM_CHAT_ID` or the app's settings).
- `scripts/backup.sh --list` lists the backups kept.

**Off-server copy** (do it weekly or before big changes), on the Mac, in the bananapatterns folder (run
`git pull` there once so the script exists; the Mac copy itself stays stopped):
```bash
scripts/pull-backups.sh      # rsync ubuntu@banana:banana-backups/ -> ~/banana-backups-cloud/
```
Optional extra: Oracle boot volume backups (Block Storage → Boot Volumes → `banana` → Backups; the Always Free
tier includes 5 volume backups).

### Restore
```bash
cd ~/bananapatterns
./docker.sh down
ls ~/banana-backups                                              # pick a day
mv data/bananapatterns.db data/bananapatterns.db.broken           # keep the old one, just in case
rm -f data/bananapatterns.db-wal data/bananapatterns.db-shm
gunzip -c ~/banana-backups/<day>/bananapatterns.db.gz > data/bananapatterns.db
# only if .env or config/ were lost too:
#   cp ~/banana-backups/<day>/env .env && chmod 600 .env
#   tar -xzf ~/banana-backups/<day>/config.tar.gz
./docker.sh up
```
Everything after the backup's night (scans, alerts sent, settings changed) is gone; the next evening scan
catches up the market data, and Kite candles are re-synced as needed.

---

## Part 5: Maintenance

| What | How often | How |
|---|---|---|
| Ubuntu security updates | automatic | `unattended-upgrades` is on by default in Ubuntu 24.04 |
| Reboot after kernel updates | when asked | `cat /var/run/reboot-required` prints a line when one is due → `sudo reboot` (weekend or after 23:30). The app and Tailscale come back on their own |
| Full update | monthly | `sudo apt-get update && sudo apt-get -y full-upgrade`, then reboot if required |
| Docker disk clean-up | monthly / after updates | `docker system df`, then `docker image prune -f` (old images from earlier builds) |
| Disk space | monthly | `df -h /` (50 GB disk; app + images + backups use a few GB) |
| Backups | weekly | `scripts/backup.sh --list`; Mac: `scripts/pull-backups.sh` |
| Oracle cost | monthly | Billing → Cost analysis = 0 (the budget alert emails you otherwise) |
| Resize the VM | if needed | Instance → Edit → Edit shape (OCPU / memory; one reboot). Stay within 4 OCPU / 24 GB in total |

Nothing expires: the Tailscale key expiry is disabled, the HTTPS certificate is renewed by Tailscale, the deploy
key has no expiry.

---

## Part 6: Troubleshooting

First look: `./docker.sh status`, `./docker.sh logs`, the dashboard **Logs** tab, `logs/bananapatterns.log`.

| Symptom | Cause / fix |
|---|---|
| Page loads, then goes **blank** | A dashboard (JavaScript) error. Open the browser console (Chrome: ⌥⌘J) and copy the red error; `./docker.sh logs` for the server side. After an update, a hard refresh (⌘⇧R) first. |
| `https://banana.<tailnet>` doesn't open | Tailscale off or another account on that device (app → Connected, `banana` listed). Server side: `tailscale status`, `tailscale serve status` (re-run `sudo tailscale serve --bg 8000`), `./docker.sh status`. |
| `ping banana` / name "Unknown host", but the 100.x IP works | MagicDNS: admin → DNS → enable; on the Mac keep "Use Tailscale DNS settings" on; reopen the app. |
| Certificate warning / HTTPS fails | Admin → DNS → **HTTPS Certificates** enabled? The first visit can take ~30 s. |
| Kite login: "invalid redirect" / lands on localhost | The Kite console's Redirect URL must be exactly `https://banana.<tailnet>/api/kite/callback`, the same as `KITE_REDIRECT_URL` in the server's `.env` (then `./docker.sh restart`). |
| Kite logged out during the day | Something else logged in with the same API key (the Mac copy, another app). Log in again; keep one copy. |
| "Kite login required" in the morning | Normal: tokens expire at 06:00 IST. Use the Telegram login link. |
| NSE panels empty (results, filings, deals) | From the server: `curl -s -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' https://nsearchives.nseindia.com/content/equities/bulk.csv`. 403 on NSE's *home page* is normal; 403 on the APIs means NSE is blocking this IP (rare; the rest of the app is unaffected). |
| No Telegram alerts | Alerts tab: token, chat, "Ready", Send test. `curl -s -o /dev/null -w '%{http_code}' https://api.telegram.org/` should be 200/302. |
| Two of every alert / Kite keeps logging out | Two copies are running. Stop the Mac one (`./docker.sh down`, launchd agent). |
| Container keeps restarting / "unhealthy" | `./docker.sh logs`: a Python error after an update → [roll back](#roll-back-to-an-earlier-version) and report it; a `.env` typo → fix and `./docker.sh restart`. |
| `./docker.sh update`: "Your local changes ... would be overwritten" / "Not possible to fast-forward" | `git status`. If it is `config/nse_holidays.json` (the app refreshes it): `git checkout -- config/nse_holidays.json`, then update again (the app fetches it again). Other files: local edits on the server; `git stash`, then update. Don't edit code on the server. |
| Build fails: "no space left on device" | `docker image prune -f`, `docker builder prune -f`; check `df -h /`. |
| Build or app killed (out of memory) | `free -h`; swap should show 2 GB (`swapon --show`); consider 12 GB memory (Edit shape). |
| SSH: "Connection refused" / timeout | VM still booting (wait a minute); public IP changed (only if the VM was recreated); use `ssh ubuntu@banana` over Tailscale instead. "Permission denied (publickey)": wrong key on this Mac. |
| Oracle: "Out of capacity" | See [1.4](#14-create-the-server): saved stack, smaller shape, retry later, PAYG. |
| Backup failure message in Telegram | `tail logs/backup.log`; run `scripts/backup.sh` by hand; check `df -h /`. A corrupt database copy means the live database needs attention: restore the last good night. |
| Server unreachable in every way | Oracle console → instance → **Reboot**; check Tailscale admin for `banana`. Meanwhile use the [Mac fallback](#fallback-to-the-mac). |

---

## Part 7: Command cheat sheet

**Mac**
```bash
ssh ubuntu@banana                     # log in to the server (over Tailscale)
ping -c 3 banana                      # is it reachable?
scripts/pull-backups.sh               # copy the server's backups to ~/banana-backups-cloud
```

**Server** (`cd ~/bananapatterns`)
```bash
./docker.sh status                    # running / healthy
./docker.sh logs                      # follow the app log (Ctrl+C stops following)
./docker.sh update                    # after a PR is merged: pull + rebuild + restart
./docker.sh restart                   # after editing .env
./docker.sh rebuild                   # rebuild the current code (rollback)
./docker.sh down / up                 # stop / start
scripts/backup.sh                     # backup now
scripts/backup.sh --list              # backups kept
tail -20 logs/backup.log              # backup history
tailscale status                      # devices on the tailnet
tailscale serve status                # the HTTPS mapping
sudo tailscale serve --bg 8000        # re-create it if missing
df -h / ; free -h                     # disk / memory
docker image prune -f                 # remove old images
cat /var/run/reboot-required          # reboot due?
```

**Web consoles**
- Oracle: [cloud.oracle.com](https://cloud.oracle.com) (instance, shape, billing, budgets)
- Tailscale: [login.tailscale.com/admin](https://login.tailscale.com/admin) (machines, DNS, HTTPS)
- Kite: [developers.kite.trade](https://developers.kite.trade) (redirect URL)
- GitHub: repository → Settings → Deploy keys
