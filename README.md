# 🎧 Audible Watcher

A small self-hosted app that watches Audible for **new releases from your
favorite authors and series** and **emails you** when something new shows up
(including pre-orders, as soon as they're listed).

![The Pre-orders tab: upcoming audiobooks from followed series and authors, each with cover, release date and a PRE-ORDER or PRE-ORDERED badge](docs/screenshots/preorders.png)

It uses Audible's public catalog API — no Audible login required, no
credentials, no scraping of web pages. Everything runs on your own computer.

**What leaves your machine**, and nothing else:

- **Audible's catalog API** — searches for the authors and series you watch.
- **Google Books and Open Library** — a book's title and first author, to
  show when it first came out in print. You can turn this off: Settings →
  Monitoring → untick "Show when books first came out in print".
- **Amazon's image servers** — your browser loads the cover pictures.
- **Amazon sign-in** — only if you connect your Audible account (optional).
- **Your own email server** — to send you the notification emails.

Optionally, you can connect your Audible account (Settings → Audible account) to see
OWNED badges on books you already have and PRE-ORDERED tags on the exact
editions you've pre-ordered. You sign in on amazon.com in your own browser;
the app never sees your password and stores only revocable device tokens
(`audible_auth.json` — treat it like a key and don't share it). **Disconnect**
in Settings removes the app's device from your Amazon account and
deletes the tokens; you can also revoke it from Amazon's "Manage Your
Content and Devices".

> **This fork** (mylacc/audible-watcher) adds two things to
> [Andrew911tt/audible-watcher](https://github.com/Andrew911tt/audible-watcher):
> [wishlist price alerts](#wishlist-price-alerts) and
> [sending through a mail relay without sign-in](#mail-relay-without-sign-in).
> Its Docker image is `ghcr.io/mylacc/audible-watcher`.

> Audible Watcher is an independent open-source project. It is not
> affiliated with or endorsed by Audible or Amazon.

## Features

- **Dashboard** showing everything you watch, with a "new books" section,
  latest release dates, pre-orders and a check history log.

  ![The dashboard's Authors tab: each watched author with their number of titles, upcoming pre-orders and latest release date](docs/screenshots/dashboard.png)

- **Watch authors or series** — search Audible from the app and add them.
  Watching a series also watches its author.
- **Daily automatic check** (time configurable) plus a "Check Audible now"
  button that runs in the background while the page updates itself.
- **Email digest** via Gmail or any other email provider when new titles
  appear, with step-by-step setup on the Settings page.
- **No false alarms**:
  - When you add a watch, everything currently on Audible is recorded as a
    baseline. Only titles that appear *after* that are flagged.
  - Foreign-language editions are filtered out, and each new book is checked
    against your Audible store before it's emailed, so other-region
    editions never reach your inbox.
  - If Audible can't be reached when you add a watch, or you switch to a
    different Audible store, the next check quietly records a new baseline
    instead of flagging everything.
- **Your library** (optional) — OWNED and PRE-ORDERED badges, and a Library
  tab with your upcoming pre-orders first. View it all at once (sorted by
  recent purchase, release date, title, author or length) or grouped by
  author or series. Books you own in another region's edition count as
  owned. The library re-syncs about once a day with the daily check, so new
  purchases show up on their own.

  ![A series page (Murderbot Diaries) in series order: owned books carry an OWNED badge, and dramatized adaptations are listed next to the original editions](docs/screenshots/series-page.png)

- **Series links** — every series name links to that series' page in the
  app (or a search, if you don't watch it yet), and series pages link to
  the series in the Audible store.
- **First published** dates: see when a book came out in print before the
  audiobook (can be turned off).
- **Safe on your network** — an optional password, protection against
  websites that try to reach the app through your browser, and an Access
  panel in Settings showing how yours is set up. See
  [Opening it from other devices](#opening-it-from-other-devices).

## Install

### Windows (installer)

1. Download **AudibleWatcher-Setup-x.y.z.exe** from the
   [latest release](https://github.com/Andrew911tt/audible-watcher/releases/latest).
2. Run it. No admin rights or Python needed. Leave **"Start Audible Watcher
   when I sign in"** ticked — the app has to be running for the daily check.
3. Audible Watcher opens in your browser at **http://localhost:5055**. It
   keeps running in the background with an icon in the system tray
   (right-click it to open the app or quit).

Windows SmartScreen may warn about an unrecognized app because the installer
isn't code-signed. Click **More info → Run anyway**, or build it yourself
from source (below).

Your data is kept in `%LOCALAPPDATA%\Audible Watcher` (tray menu → *Open
data folder*). Uninstalling leaves it in place.

### Docker (NAS / home server)

A ready-made image is published with every release, so you only need a
`docker-compose.yml`:

```yaml
services:
  audible-watcher:
    image: ghcr.io/mylacc/audible-watcher:latest
    container_name: audible-watcher
    restart: unless-stopped
    ports: ["5055:5055"]
    environment:
      - TZ=America/Los_Angeles   # your time zone: the daily check uses it
    volumes:
      - ./data:/data
```

```
mkdir -p data && sudo chown 1000:1000 data
docker compose up -d
```

Then open **http://your-server:5055**. Data is kept in `./data`. To update
later: `docker compose pull && docker compose up -d`.

Prefer to build it yourself? Clone the repository, copy
`docker-compose.example.yml` to `docker-compose.yml` (it builds from
source) and run `docker compose up -d`.

**Upgrading a Docker install from 1.0.0 or 1.0.1:** since 1.0.2 the
container runs as an unprivileged user (uid 1000) instead of root. Give it
the data folder once before starting the new version:
`sudo chown -R 1000:1000 ./data`. If you forget, the container stops with
a message saying exactly that.

### From source (Windows, macOS, Linux)

Requires Python 3.10+ ([python.org](https://www.python.org/downloads/) —
on Windows, check "Add Python to PATH" during install).

```
git clone https://github.com/mylacc/audible-watcher.git
cd audible-watcher
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:5055**. When run from source, data is kept next
to `app.py` (`watcher.db`, `audible_auth.json`).

- Windows: double-click `start.bat` (after the one-time `pip install`).
- macOS / Linux: `./start.sh`
- Prefer the tray app? `pip install -r requirements-desktop.txt` then
  `python desktop.py`.

## Set up email

The **Settings** page walks you through this step by step, with links to
the right Google pages and a troubleshooting list. In short, for Gmail:

Gmail doesn't let apps use your normal password, so you create an
**app password** (a 16-letter password just for this app, which you can
revoke anytime without changing your real password):

1. Turn on **2-Step Verification** at
   https://myaccount.google.com/signinoptions/twosv (app passwords
   require it).
2. Create an app password named "Audible Watcher" at
   https://myaccount.google.com/apppasswords and copy the code. Pasting it
   with the spaces Google shows is fine.
3. In Audible Watcher → **Settings**: enter your Gmail address, paste the app
   password, set "Send notifications to", tick **Send me an email**, save,
   then click **Send test email**.

**Other providers:** Settings has a table of Yahoo Mail, iCloud Mail,
Fastmail and Zoho Mail with a **Use** button that fills in the server
settings; those providers need an app password too. Any other SMTP server
works if you enter its server, port and security setting. Outlook.com /
Hotmail usually won't work, because Microsoft is phasing out password
sign-in for apps like this one.

If a test email fails, the error message says what to fix (for example
"use an app password, not your normal password").

### Mail relay without sign-in

If you run your own mail relay (for example a Postfix container on your
NAS that forwards to your real mail provider), leave **Email address** and
**App password** blank, set **Security** to *None* (plain SMTP, usually
port 25) or *STARTTLS* if your relay offers it, and fill in both **From
address** and **Send notifications to**. If you fill in an email address
and app password, the app signs in as before.

## Wishlist price alerts

With your Audible account connected, the app reads your Audible wishlist
and its current prices, and emails you when a title costs your alert
price or less.

- **Default alert price:** Settings → Monitoring → *Wishlist alert price*.
  Blank means no default.
- **Per-title prices:** the dashboard's **Wishlist** tab lists every title
  with its price now, list price and lowest price seen; type a price next
  to a title to override the default for it (blank = use the default).
- **When it checks:** with the daily check, and again 12 hours later, so a
  one-day sale is caught whenever it starts. *Refresh prices now* on the
  Wishlist tab checks immediately.
- **What "price" means:** Audible's *lowest price* for the title on your
  account: a sale or member price when there is one, otherwise the list
  price. Credit prices are ignored.
- **No repeats:** one email when a title reaches its alert price, another
  only if it gets cheaper still. Once its price goes back above the alert
  price, the alert re-arms. Changing an alert price re-arms it too.
  Titles you already own are skipped.
- Email follows the same switch as new-release emails: with email off,
  matches show only on the Wishlist tab.

Disconnecting your Audible account deletes the stored wishlist.

## Run it automatically (source / server installs)

The Windows installer and Docker handle this for you. Otherwise:

**Windows** — Task Scheduler → Create Basic Task → trigger *When I log on* →
*Start a program*: `pythonw.exe`, arguments `app.py`, start in the
`audible-watcher` folder.

**Linux** — systemd (edit the user and paths in the file first):
```
sudo cp audible-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now audible-watcher
```

**macOS** — add `start.sh` as a Login Item, or use `launchd`.

## Settings via environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AW_HOST` | `127.0.0.1` | Address to listen on. `0.0.0.0` makes it reachable from other devices on your network. There's no login unless you set `AW_PASSWORD`, so only do this on a trusted network. |
| `AW_PORT` | `5055` | Port. |
| `AW_DATA_DIR` | see above | Folder for `watcher.db` and `audible_auth.json`. |
| `AW_ALLOWED_HOSTS` | *(empty)* | Extra hostnames you open the app by, comma-separated (e.g. `nas.example.com`). IP addresses, `localhost`, names without a dot and `.local` / `.lan` / `.home` / `.home.arpa` / `.internal` names always work. Other names are refused, which blocks websites that try to reach the app through your browser (DNS rebinding). See [Opening it from other devices](#opening-it-from-other-devices). |
| `AW_PASSWORD` | *(empty)* | Set it to require a password to open the app. Recommended if anyone you don't trust can reach it. See [Opening it from other devices](#opening-it-from-other-devices). |
| `AW_PROXY_COUNT` | `0` | Only if the app sits behind a reverse proxy (nginx, Caddy, Traefik…): how many proxies are in front of it. The app then sees each visitor's real address, so a wrong-password lockout only affects that visitor. Leave it at 0 otherwise: it makes the app trust forwarded headers. |

How to set an environment variable depends on how you installed the app —
the next section has the exact steps for each.

## Opening it from other devices

By default Audible Watcher only answers on the computer it runs on
(`http://localhost:5055`), and there's nothing to set up. The Docker image
is the exception: it's reachable from your network out of the box.

Three settings control access from other devices. You can check how yours
are set at the bottom of the **Settings** page.

| Setting | When you need it |
|---|---|
| `AW_HOST=0.0.0.0` | To open the app from a phone, tablet or another computer at all. (Docker already does this.) |
| `AW_ALLOWED_HOSTS` | Only if you open the app by a **full domain name**, like `nas.example.com` or `audible.mydomain.net`. IP addresses (`http://192.168.1.20:5055`), `localhost`, single names (`http://nas:5055`) and `.local` / `.lan` / `.home` / `.home.arpa` / `.internal` names always work. |
| `AW_PASSWORD` | If anyone you don't fully trust can reach the app: shared Wi-Fi, housemates, or any access from outside your home. Without it, anyone who can reach the port can use the app. |

**Why the hostname check?** A malicious website can point its own domain
name at your server and then use your browser to talk to Audible Watcher
("DNS rebinding"). Those requests carry the website's name, so refusing
names the app doesn't recognise stops it. If you open the app by a name
it doesn't know, you'll see an *“Address not recognised”* page; add that
name to `AW_ALLOWED_HOSTS` as shown below.

Several names: separate them with commas, e.g.
`AW_ALLOWED_HOSTS=nas.example.com,audible.example.net`.

### Docker Compose

In `docker-compose.yml`, add the lines under `environment:` (two are
already there, commented out), then run `docker compose up -d`:

```yaml
    environment:
      - TZ=America/Los_Angeles
      - AW_ALLOWED_HOSTS=nas.example.com
      - AW_PASSWORD=choose-a-password
```

### docker run

Add `-e` options and recreate the container:

```
docker run -d -p 5055:5055 -v ./data:/data \
  -e AW_ALLOWED_HOSTS=nas.example.com \
  -e AW_PASSWORD=choose-a-password \
  audible-watcher
```

### Linux service (systemd)

```
sudo systemctl edit audible-watcher
```

In the editor that opens, add the lines you need, save and close:

```ini
[Service]
Environment=AW_HOST=0.0.0.0
Environment=AW_ALLOWED_HOSTS=nas.example.com
Environment=AW_PASSWORD=choose-a-password
```

Then `sudo systemctl restart audible-watcher`.

### Windows installer

The desktop app only answers on this PC, so you normally don't need any of
this. To use it from other devices anyway:

1. Open **Start**, search for **“Edit environment variables for your
   account”** and open it.
2. Click **New…** and add `AW_HOST` = `0.0.0.0`. Add `AW_PASSWORD` the same
   way, and `AW_ALLOWED_HOSTS` if you'll use a full domain name.
3. Right-click the Audible Watcher tray icon → **Quit**, then start it again
   from the Start menu. Windows may ask to allow it through the firewall.

### From source

macOS / Linux — put the settings in front of the start command:

```
AW_HOST=0.0.0.0 AW_PASSWORD=choose-a-password ./start.sh
```

Windows — add `set` lines to `start.bat` before `python app.py`:

```bat
set AW_HOST=0.0.0.0
set AW_PASSWORD=choose-a-password
python app.py
```

**Signing in:** with `AW_PASSWORD` set, every device asks for the password
once and then stays signed in for 30 days (**Sign out** is in the menu).
Changing the password signs every device out. After 5 wrong passwords from
one address it has to wait before trying again (30 seconds, doubling up to
15 minutes); other addresses aren't affected. Behind a reverse proxy, set
`AW_PROXY_COUNT` (see the settings table) so each visitor is counted
separately.

## Notes

- The daily check runs at the hour set in Settings (default 8 AM, local time
  of the machine running the app). Times on the pages are shown in that
  local time too.
- If Audible can't be reached for several watches in a row, the check stops
  early instead of waiting out every timeout, notes it under Recent checks,
  and tries again an hour later.
- Back up `watcher.db` to keep your watch list; delete it to start fresh.
- Your email app password is stored in that local database in plain text —
  fine for a personal machine, but don't share the file. It's never shown
  on any page, and it's cleared if you change the mail server or email
  address, so it can only ever be sent to the server you entered it for.
  You can revoke the app password anytime from your email provider.
- Email off: books found while email is switched off show on the dashboard
  only. They aren't saved up for one huge email when you switch it on, and
  books you've already marked seen are never emailed.
- Marketplace: defaults to Audible US (audible.com). Change it in Settings if
  you shop on a different regional store. Each store lists many books (and
  series) under different IDs, so after a change the next check looks each
  series up in the new store, quietly re-records all your watches there and
  hides the old store's editions; nothing is flagged as new. A series the
  new store doesn't carry shows a warning on the dashboard.
- Search results show **Watching ✓** for authors and series you already
  watch.
- Author watches cover all of an author's titles and match the exact name
  Audible credits (ignoring dots, spaces and capitals), including
  co-written books. Books where the author only wrote a foreword, an
  introduction or a translation aren't included. If an author watch finds
  nothing, it shows a warning to check the spelling.
- Books Audible lists under your watches but doesn't sell in your store
  (usually another region's edition) are hidden. They're checked again
  about once a week: if your store starts selling one, it comes back as a
  new book and is emailed. Each watch's **Hidden entries** list says why
  every book is hidden, and books you hide or unhide yourself stay that
  way.
- Updating Audible Watcher never floods you with "new" books: if an update
  changes what the app fetches, the extra older titles it finds are added
  quietly. Recent and upcoming releases still alert, and so does any book
  that's genuinely new on Audible, whatever its release date.
- If one watch turns up more than 10 new books in a single check (say
  Audible adds an author's back catalogue), the email sums them up in one
  line instead of listing them all; they're all on the dashboard.
- A book that's in two series shows its number in each series on that
  series' page.
- A book that matches two watches (e.g. the author *and* the series) shows
  once on the dashboard, pre-orders and in emails, credited to the series.
  Each watch's own page still lists it. Marking it seen clears it everywhere.
- Books announced without a release date get a grey **TBA** badge. They
  aren't emailed until Audible gives them a date and they can be checked
  against your store.

## Development

```
pip install -r requirements-dev.txt
python -m pytest tests
```

The tests use a mocked Audible API (and fake email and metadata servers),
so no network is needed, and they never touch a real Audible login.

**Changing how watches fetch?** If a change makes watches see more or
different titles (a bigger window, a different search), bump
`FETCH_REVISION` in `audible_api.py`. Each watch's first check after the
update then records the extra titles quietly instead of flagging them as
new releases.

GitHub Actions runs the tests on every push: on Windows and Linux with
Python 3.12, plus Linux with Python 3.10 (the oldest supported) and 3.14.
It also builds the Docker image and checks that the container starts,
runs as a non-root user, can write its data folder and refuses unknown
hostnames.

**Live catalog tests:** a few read-only checks against Audible's real
catalog (well-known authors and series must be found and fetched), to catch
changes on Audible's side that the fake catalog can't. They run weekly on
GitHub Actions, or locally with:

```
AW_LIVE_TESTS=1 python -m pytest live_tests
```

**Building the Windows installer yourself:** install Python 3.12 and
[Inno Setup 6](https://jrsoftware.org/isinfo.php), then run
`powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -Version 1.0.0`.
The installer lands in `packaging\Output\`.

**Releases:** pushing a tag like `v1.0.0` makes GitHub Actions run the full
test suite (every OS and Python version, plus the Docker check), then build
the installer, attach it to a new release and publish the Docker image to
`ghcr.io`. Nothing is published if any test fails.

Pull requests and forks are welcome.

## License

[MIT](LICENSE)
