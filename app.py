"""Audible Watcher — monitors Audible for new releases from your favorite
authors and series, shows them in a web GUI, and emails you when something
new appears.

Run:  python app.py          (then open http://localhost:5055)
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import (Flask, flash, g, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup

import account
import audible_api
import checker
import db
import wishlist
from paths import RESOURCE_DIR, check_data_dir_writable

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("watcher")

app = Flask(__name__,
            template_folder=os.path.join(RESOURCE_DIR, "templates"),
            static_folder=os.path.join(RESOURCE_DIR, "static"))
check_data_dir_writable()
db.init_db()
# random per install, kept in the database so sessions survive restarts
app.secret_key = db.secret_key()


def _proxy_count() -> int:
    """How many reverse proxies sit in front of the app (AW_PROXY_COUNT).
    Only then are X-Forwarded-For/-Host/-Proto trusted, for exactly that
    many hops: otherwise anyone could fake their address or hostname."""
    try:
        return max(0, int(os.environ.get("AW_PROXY_COUNT", "0") or 0))
    except ValueError:
        log.warning("AW_PROXY_COUNT must be a number; ignoring it")
        return 0


if _proxy_count():
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_count(),
                            x_proto=_proxy_count(), x_host=_proxy_count())
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

_check_lock = threading.Lock()
_check_thread: threading.Thread | None = None  # the latest "Check now" run
scheduler = BackgroundScheduler()

DEFAULT_CHECK_HOUR = 8


# ------------------------------------------------------------------ access
# 1. Allowed hostnames. A "DNS rebinding" page points its own domain at this
#    server, so the browser treats it as same-site and the CSRF token can't
#    help. Its requests still carry the attacker's hostname, so refusing
#    hostnames that aren't local blocks it. Your own names: AW_ALLOWED_HOSTS.
# 2. Optional password (AW_PASSWORD) for anyone exposing the app more widely.

LOCAL_HOST_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal",
                       ".localhost")


README_ACCESS_URL = ("https://github.com/mylacc/audible-watcher"
                     "#opening-it-from-other-devices")


def _extra_hosts() -> list[str]:
    """Hostnames from AW_ALLOWED_HOSTS, lower-cased, in order."""
    return [h.strip().lower()
            for h in os.environ.get("AW_ALLOWED_HOSTS", "").split(",")
            if h.strip()]


def access_info() -> dict:
    """How access is set up, for the read-only panel on the Settings page.
    It only shows the settings: they're changed outside the app, so a
    page can never switch its own protection off."""
    return {
        "listen": HOST,
        "network": HOST not in ("127.0.0.1", "localhost", "::1"),
        "address": request.host,
        "extra_hosts": _extra_hosts(),
        "password": bool(_password()),
        "help_url": README_ACCESS_URL,
    }


def host_allowed(host: str) -> bool:
    host = (host or "").strip().lower()
    if host.startswith("["):              # [::1]:5055
        host = host[1:host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:            # name:port
        host = host.split(":")[0]
    if not host:
        return False
    extra = set(_extra_hosts())
    if "*" in extra or host in extra:
        return True
    try:
        ipaddress.ip_address(host)        # plain IP address
        return True
    except ValueError:
        pass
    return "." not in host or host.endswith(LOCAL_HOST_SUFFIXES)


@app.before_request
def check_host():
    # static files (the stylesheet) reveal nothing, and the refusal page
    # needs its stylesheet to be readable
    if request.endpoint == "static" or host_allowed(request.host):
        return None
    # Werkzeug blanks hostnames with illegal characters; show a placeholder
    name = request.host.rsplit(":", 1)[0] or "your-server-name"
    log.warning("Refused request for unrecognised host %r", request.host)
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": False,
                "error": f"Address “{name}” not allowed (AW_ALLOWED_HOSTS)"}, 400
    # the name comes from the request: the template escapes it
    return render_template("host_refused.html", name=name,
                           help_url=README_ACCESS_URL), 400


def _password() -> str:
    return os.environ.get("AW_PASSWORD", "")


def _password_tag() -> str:
    """Stored in the session at sign-in. Changing AW_PASSWORD changes it,
    which signs everyone out."""
    return hmac.new(app.secret_key.encode(), _password().encode(),
                    hashlib.sha256).hexdigest()[:32]


def _signed_in() -> bool:
    return bool(_password()) and secrets.compare_digest(
        session.get("signed_in", ""), _password_tag())


@app.before_request
def require_sign_in():
    if not _password() or request.endpoint in ("login", "static",
                                                "healthz"):
        return None
    if _signed_in():
        return None
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": False, "error": "Please sign in again."}, 401
    target = (request.full_path.rstrip("?")
              if request.method == "GET" else None)
    return redirect(url_for("login", next=target))


def _safe_next(target: str | None) -> str:
    """Only a path on this site. Browsers drop tabs and newlines and treat
    a backslash like a slash, so "/\t/evil.example" would become
    "//evil.example" (another site): anything with those is refused."""
    if (not target or not target.startswith("/")
            or any(ch in target for ch in "\\\t\r\n")
            or any(ord(ch) < 0x20 for ch in target)
            or target.startswith("//")):
        return url_for("dashboard")
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return url_for("dashboard")
    return target


# Wrong passwords: after FREE_ATTEMPTS failures from one address, it must
# wait, doubling each time (30 s, 1 min, 2 min ... up to 15 min). Counted
# per address only: a shared limit would let anyone lock everyone out.
# Nothing sleeps, so a flood of attempts can't stall the app. Behind a
# reverse proxy, AW_PROXY_COUNT makes the address the visitor's own.
FREE_ATTEMPTS = 5
FIRST_LOCKOUT = 30          # seconds
MAX_LOCKOUT = 15 * 60
FORGET_AFTER = 24 * 3600    # an address's failures are forgotten after this
MAX_TRACKED = 10_000        # never remember more addresses than this
_login_lock = threading.Lock()
# key -> (failures, locked until, last failure), times from time.monotonic()
_login_failures: dict[str, tuple[int, float, float]] = {}


def _lockout_left(key: str) -> int:
    with _login_lock:
        _, until, _ = _login_failures.get(key, (0, 0.0, 0.0))
    return max(0, int(until - time.monotonic() + 0.999))


def _forget_old_failures(now: float):
    """Drop addresses that last failed long ago and aren't locked; if still
    too many, drop the oldest. (Caller holds _login_lock.)"""
    stale = [k for k, (_, until, last) in _login_failures.items()
             if until <= now and now - last > FORGET_AFTER]
    for k in stale:
        del _login_failures[k]
    if len(_login_failures) > MAX_TRACKED:
        oldest = sorted(_login_failures, key=lambda k: _login_failures[k][2])
        for k in oldest[:len(_login_failures) - MAX_TRACKED]:
            del _login_failures[k]


def _record_failure(key: str, free: int = FREE_ATTEMPTS):
    with _login_lock:
        now = time.monotonic()
        count, _, _ = _login_failures.get(key, (0, 0.0, 0.0))
        count += 1
        until = 0.0
        if count > free:
            wait = min(FIRST_LOCKOUT * 2 ** (count - free - 1), MAX_LOCKOUT)
            until = now + wait
        _login_failures[key] = (count, until, now)
        _forget_old_failures(now)  # after adding: never above MAX_TRACKED


def _clear_failures(key: str):
    with _login_lock:
        _login_failures.pop(key, None)


app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


# ------------------------------------------------------------------ CSRF
# Without a login (the default), any web page the user visits could
# make their browser POST here (change settings, send the saved SMTP
# password to another server via "Send test email", delete watches...).
# Every state-changing request must carry the token from the user's session.

def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = session["csrf_token"] = secrets.token_urlsafe(32)
    return token


def csrf_field() -> Markup:
    return Markup('<input type="hidden" name="csrf_token" value="%s">'
                  % csrf_token())


app.jinja_env.globals["csrf_field"] = csrf_field


@app.before_request
def csrf_protect():
    if request.method != "POST":
        return None
    sent = (request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token") or "")
    expected = session.get("csrf_token") or ""
    # compare as bytes: compare_digest refuses non-ASCII str
    if expected and secrets.compare_digest(sent.encode(), expected.encode()):
        return None
    log.warning("Rejected POST %s without a valid CSRF token", request.path)
    msg = ("That didn't go through: the page was out of date (or the "
           "request didn't come from Audible Watcher). Nothing was "
           "changed. Please try again.")
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": False, "error": msg}, 400
    # back to the page it came from (only if that's this site), with a note
    flash(msg, "error")
    back = request.referrer or ""
    parts = urlsplit(back)
    target = (_safe_next(parts.path + (f"?{parts.query}" if parts.query else ""))
              if parts.netloc == request.host else url_for("dashboard"))
    return redirect(target)


# ------------------------------------------------------------ templates
# Lookups templates need, computed lazily and at most once per request
# (a page with 100 book cards used to re-query the watch list each render).

def _watches_by_key() -> dict:
    if "watches_by_key" not in g:
        g.watches_by_key = {(w["type"], w["name"].strip().casefold()):
                            w["id"] for w in db.list_watches()}
    return g.watches_by_key


def _book_key(title, authors) -> tuple | None:
    """Edition-independent identity: normalised title + first author. Other
    regions' editions of a book have their own ASIN but the same key."""
    title = "".join(ch for ch in (title or "").lower() if ch.isalnum())
    author = audible_api.norm_name(audible_api.first_author(authors) or "")
    return (title, author) if title else None


def _owned_index() -> tuple[set, set]:
    if "owned_index" not in g:
        keys = {_book_key(b["title"], b["authors"])
                for b in db.owned_books_list()}
        keys.discard(None)
        g.owned_index = (db.owned_asins(), keys)
    return g.owned_index


def ownership(book) -> str | None:
    """'preordered', 'owned' or None for a catalog book.

    An exact ASIN match is yours: pre-ordered if it isn't out yet. A title
    and author match (another region's edition) only counts once the book
    is released: an upcoming edition of a book you own is a different
    product you haven't ordered.
    """
    asins, keys = _owned_index()
    release = book.get("release_date")
    upcoming = not release or release > date.today().isoformat()
    if book.get("asin") in asins:
        return "preordered" if upcoming else "owned"
    key = _book_key(book.get("title"), book.get("authors"))
    if not upcoming and key is not None and key in keys:
        return "owned"
    return None


def is_owned(book) -> bool:
    return ownership(book) is not None


def has_owned() -> bool:
    return bool(_owned_index()[0])


def watch_id_for(wtype, name):
    """The existing watch's id for (type, name), or None."""
    return _watches_by_key().get((wtype, (name or "").strip().casefold()))


def audible_series_url(series_asin, series_title=""):
    """The series' page in the user's Audible store."""
    return audible_api.series_page_url(
        db.get_settings().get("marketplace", "us"), series_asin, series_title)


def series_url(name):
    """The series' watch page if watched, else an in-app search for it
    (where it can be watched)."""
    name = (name or "").strip()
    watch_id = watch_id_for("series", name)
    if watch_id:
        return url_for("watch_detail", watch_id=watch_id)
    return url_for("search", q=name, type="series")


def author_url(name):
    """The author's watch page if watched, else an in-app author search."""
    name = name.strip()
    watch_id = watch_id_for("author", name)
    if watch_id:
        return url_for("watch_detail", watch_id=watch_id)
    return url_for("search", q=name, type="author")


app.jinja_env.globals.update(split_authors=audible_api.split_authors,
                             ownership=ownership, is_owned=is_owned,
                             has_owned=has_owned,
                             watch_id_for=watch_id_for, author_url=author_url,
                             series_url=series_url,
                             audible_series_url=audible_series_url)
app.jinja_env.tests["owned"] = is_owned


@app.template_filter("hours")
def hours_filter(minutes) -> str:
    """725 -> '12h 05m'."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return ""
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


@app.template_filter("localtime")
def localtime_filter(value, fallback="never"):
    """'2026-11-15T21:41:14Z' (stored UTC) -> '2026-11-15 13:41' in the
    local time of the machine running the app."""
    if not value:
        return fallback
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().strftime("%Y-%m-%d %H:%M")


@app.template_filter("money")
def money_filter(amount, currency=None) -> str:
    return wishlist.format_price(amount, currency)


@app.context_processor
def inject_page_state():
    return {
        # for marking unreleased/pre-order books (release date in the future)
        "today": date.today().isoformat(),
        # pages auto-refresh while a check runs in the background
        "check_running": _check_lock.locked(),
        # show "Sign out" only when a password is set
        "password_enabled": bool(_password()),
    }


RETRY_AFTER_OUTAGE = timedelta(hours=1)


def _run_check(trigger: str):
    """Run a full check; the caller must already hold _check_lock. If
    Audible was unreachable, try once more an hour later."""
    try:
        result = checker.check_all(trigger=trigger)
        _refresh_library_if_stale()
        if not result.get("aborted"):
            wishlist.check(trigger)
        if result.get("aborted") and trigger != "retry":
            when = datetime.now() + RETRY_AFTER_OUTAGE
            scheduler.add_job(_retry_check, "date", run_date=when,
                              id="retry_check", replace_existing=True)
            log.info("Audible unreachable; retrying at %s",
                     when.strftime("%H:%M"))
    except Exception:  # noqa: BLE001 - logged; the check log shows the rest
        log.exception("%s check failed", trigger.capitalize())
    finally:
        _check_lock.release()


LIBRARY_REFRESH = timedelta(hours=20)


def _refresh_library_if_stale():
    """Re-sync the connected Audible library about once a day (with the
    daily check), so OWNED / PRE-ORDERED badges follow new purchases."""
    if not account.is_connected():
        return
    last = db.get_settings().get("library_synced_at")
    try:
        synced = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        synced = None
    if synced and datetime.now(timezone.utc) - synced < LIBRARY_REFRESH:
        return
    try:
        n = account.sync_library()
        log.info("Library synced: %d owned title(s)", n)
    except Exception:  # noqa: BLE001 - badges just stay as they were
        log.warning("Library sync failed; trying again next check",
                    exc_info=True)


def scheduled_check():
    if _check_lock.acquire(blocking=False):
        _run_check("scheduled")


def scheduled_price_check():
    """The extra wishlist-only check, half a day after the daily check, so
    a one-day sale is caught whichever hour it starts."""
    if not account.is_connected():
        return
    if _check_lock.acquire(blocking=False):
        try:
            wishlist.check("scheduled")
        finally:
            _check_lock.release()


def _retry_check():
    if _check_lock.acquire(blocking=False):
        _run_check("retry")


def _parse_hour(value) -> int | None:
    """A valid daily-check hour (0-23), or None."""
    value = str(value or "").strip()
    if value.isdigit() and 0 <= int(value) <= 23:
        return int(value)
    return None


def reschedule():
    raw = db.get_settings().get("check_hour")
    hour = _parse_hour(raw)
    if hour is None:  # never let a bad stored value stop the app starting
        log.warning("Invalid check_hour %r in settings; using %02d:00",
                    raw, DEFAULT_CHECK_HOUR)
        hour = DEFAULT_CHECK_HOUR
    # replace only the daily job, so a pending outage retry survives
    try:
        scheduler.remove_job("daily_check")
    except JobLookupError:
        pass
    scheduler.add_job(scheduled_check, CronTrigger(hour=hour, minute=0),
                      id="daily_check")
    try:
        scheduler.remove_job("price_check")
    except JobLookupError:
        pass
    scheduler.add_job(scheduled_price_check,
                      CronTrigger(hour=(hour + 12) % 24, minute=0),
                      id="price_check")
    log.info("Daily check scheduled for %02d:00 local time (wishlist prices "
             "also at %02d:00)", hour, (hour + 12) % 24)


# ------------------------------------------------------------------ pages

@app.route("/")
def dashboard():
    tab = request.args.get("tab", "authors")
    if tab not in ("authors", "series", "preorders", "library", "wishlist"):
        tab = "authors"
    view = request.args.get("view", "all")
    if view not in ("all", "author", "series"):
        view = "all"
    sort = request.args.get("sort", "recent")
    allowed = (ALL_BOOKS_SORTS if view == "all"
               else ("recent", "newest", "largest", "owned"))
    if sort not in allowed:
        sort = "recent"
    watches = db.list_watches()
    fresh = db.new_books()
    checks = db.recent_checks(5)
    settings = db.get_settings()
    owned = db.owned_books_list() if tab == "library" else []
    if view == "all":
        owned = _sort_all_books(owned, sort)
    return render_template(
        "dashboard.html", tab=tab, view=view, sort=sort, fresh=fresh,
        checks=checks, settings=settings,
        authors=[w for w in watches if w["type"] == "author"],
        series=[w for w in watches if w["type"] == "series"],
        preorders=db.preorder_books(),
        owned=owned, owned_total=db.owned_count(),
        owned_groups=_group_owned(owned, view, sort),
        connected=account.is_connected(),
        wishlist_total=db.wishlist_count(),
        **(_wishlist_context(settings) if tab == "wishlist" else {}))


def _wishlist_context(settings: dict) -> dict:
    """The Wishlist tab: titles at or below their alert price first
    (cheapest first), then the rest in wishlist order."""
    default = wishlist.default_target(settings)
    owned = db.owned_asins()
    items = []
    for i in db.wishlist_items():
        i["target"] = wishlist.effective_target(i, default)
        i["hit"] = wishlist.at_or_below(i, default)
        i["owned"] = i["asin"] in owned
        items.append(i)
    items.sort(key=lambda i: (not i["hit"],
                              i["price"] if i["hit"] else 0))
    return {"wishlist_items": items, "price_default": default,
            "wishlist_checked_at": settings.get("wishlist_checked_at"),
            "wishlist_error": settings.get("wishlist_error")}


# Library → All books: sort options (the first is the default)
ALL_BOOKS_SORTS = ("recent", "release", "title", "author", "length")
_LEADING_ARTICLE = ("the ", "a ", "an ")


def _title_key(b: dict) -> str:
    """Title for sorting, ignoring a leading "The", "A" or "An"."""
    t = (b.get("title") or "").strip().lower()
    for art in _LEADING_ARTICLE:
        if t.startswith(art):
            return t[len(art):]
    return t


def _surname_key(b: dict) -> tuple:
    """First author by surname: "Lee Child" -> ("child", "lee child");
    "Martin Luther King, Jr." sorts under King."""
    name = (audible_api.first_author(b.get("authors")) or "").split(",")[0]
    parts = name.strip().lower().split()
    return (parts[-1] if parts else "~", name.lower())


def _sort_all_books(owned: list[dict], sort: str) -> list[dict]:
    """Order the Library's All books view. 'recent' keeps the database
    order: upcoming pre-orders first, then newest purchase first."""
    if sort == "release":      # newest release first; pre-orders on top
        return sorted(owned, key=lambda b: (b.get("release_date") or "9999",
                                            _title_key(b)), reverse=True)
    if sort == "title":
        return sorted(owned, key=_title_key)
    if sort == "author":       # by surname, then series order, then title
        return sorted(owned, key=lambda b: (_surname_key(b),
                                            b.get("series_title") or "~",
                                            _seq_key(b), _title_key(b)))
    if sort == "length":       # longest listen first
        return sorted(owned, key=lambda b: (-(b.get("runtime_minutes") or 0),
                                            _title_key(b)))
    return owned


def _seq_key(b: dict) -> tuple:
    """Sort key for a book's position in its series (handles '3.5' and
    multi-sequence strings like '1.5, 4.4'; unnumbered books go last)."""
    s = (b.get("series_sequence") or "").split(",")[0].strip()
    try:
        return (0, float(s))
    except ValueError:
        return (1, 0.0)


def _group_owned(owned: list[dict], view: str,
                 sort: str = "recent") -> list[tuple] | None:
    """Group the owned library for the Library tab's sub-views."""
    if view == "author":
        groups: dict[str, list] = {}
        for b in owned:
            key = audible_api.first_author(b["authors"]) or "Unknown"
            groups.setdefault(key, []).append(b)
        for grp in groups.values():  # newest release first within an author
            grp.sort(key=lambda b: b["release_date"] or "9999", reverse=True)
        return sorted(groups.items(), key=lambda kv: kv[0].lower())
    if view == "series":
        groups = {}
        for b in owned:
            groups.setdefault(b["series_title"] or "Standalone", []).append(b)
        for grp in groups.values():  # book order within a series
            grp.sort(key=lambda b: (_seq_key(b), b["release_date"] or ""))
        if sort == "newest":       # series with the newest book first
            def key(kv):
                return max((b["release_date"] or "9999") for b in kv[1])
        elif sort == "largest":    # biggest series first (full catalog size)
            sizes = db.series_sizes()
            def key(kv):
                return max(sizes.get(kv[0], 0), len(kv[1]))
        elif sort == "owned":      # series you own the most of first
            def key(kv):
                return len(kv[1])
        else:                      # most recent purchase first
            def key(kv):
                return max((b["purchase_date"] or "") for b in kv[1])
        ordered = sorted(groups.items(), key=key, reverse=True)
        ordered.sort(key=lambda kv: kv[0] == "Standalone")  # always last
        return ordered
    return None


WATCH_SERIES_SORTS = ("series", "published")


def _series_order_key(b: dict) -> tuple:
    """Series order: numbered books by number (unnumbered last), then the
    versions of one book by release date (undated last), then title."""
    return (_seq_key(b), b.get("release_date") or "9999",
            (b.get("title") or "").casefold())


def _publication_key(b: dict) -> tuple:
    """Publication order: oldest release first; not yet dated last."""
    return (b.get("release_date") or "9999", _seq_key(b),
            (b.get("title") or "").casefold())


@app.route("/watch/<int:watch_id>")
def watch_detail(watch_id):
    w = db.get_watch(watch_id)
    if not w:
        flash("Watch not found", "error")
        return redirect(url_for("dashboard"))
    every = db.books_for_watch(watch_id, include_hidden=True)
    books = [b for b in every if not b["hidden"]]
    hidden_books = [b for b in every if b["hidden"]]
    view = request.args.get("view", "all")
    sort = request.args.get("sort", "series")
    series_groups = None
    if w["type"] == "series":
        # one series: no grouping, just its books in series order (book 1
        # first, 2.5 between 2 and 3; versions of a book by release date)
        # or in publication order (oldest first)
        view = "all"
        if sort not in WATCH_SERIES_SORTS:
            sort = "series"
        books = (sorted(books, key=_publication_key) if sort == "published"
                 else sorted(books, key=_series_order_key))
    elif view == "series":
        groups: dict[str, list] = {}
        for b in books:
            groups.setdefault(b["series_title"] or "Standalone", []).append(b)
        for grp in groups.values():  # each series in series order
            grp.sort(key=_series_order_key)
        # series ordered by their individually newest book
        series_groups = sorted(
            groups.items(),
            key=lambda kv: max((b["release_date"] or "9999") for b in kv[1]),
            reverse=True)
    marketplace = db.get_settings().get("marketplace", "us")
    audible_url = (audible_api.series_page_url(marketplace, w.get("asin"),
                                               w["name"])
                   if w["type"] == "series" else None)
    return render_template("watch.html", watch=w, books=books,
                           hidden_books=hidden_books, view=view, sort=sort,
                           series_groups=series_groups,
                           audible_url=audible_url,
                           settings_language=(db.get_settings().get(
                               "language") or "").strip().lower())


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    results, series_results = [], []
    if q:
        settings = db.get_settings()
        mp = settings.get("marketplace", "us")
        lang = (settings.get("language") or "").strip().lower()
        try:
            # one query, both kinds of matches — the results speak for
            # themselves (an author name yields authors, a series name series)
            # relevance order: the newest products of a widely translated
            # author are mostly translations, which the language filter
            # would then remove entirely
            books = audible_api.search_by_author(q, mp, max_results=50,
                                                 sort=None)
            books = [b for b in books
                     if audible_api.matches_language(b, lang)
                     and not audible_api.is_podcast(b)]
            names = {}
            for b in books:
                for name in b["author_list"]:  # "Name, Jr." stays whole
                    name = name.strip()
                    if audible_api.is_role_credit(name):
                        continue  # "X - introduction" isn't an author
                    if name and q.lower() in name.lower():
                        names.setdefault(name, {"name": name, "count": 0,
                                                "sample": b})
                        names[name]["count"] += 1
            results = sorted(names.values(), key=lambda x: -x["count"])
            series_results = audible_api.search_series_candidates(
                q, mp, language=lang)
        except audible_api.AudibleAPIError as e:
            flash(f"Audible search failed: {e}", "error")
    return render_template("search.html", q=q,
                           results=results, series_results=series_results)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        keys = ["marketplace", "language", "check_hour", "smtp_host",
                "smtp_port", "smtp_security", "smtp_user", "email_from",
                "email_to", "price_threshold"]
        values = {k: request.form.get(k, "").strip() for k in keys}
        if _parse_hour(values["check_hour"]) is None:
            flash("Daily check time must be a whole hour from 0 to 23 — "
                  "settings not saved", "error")
            return redirect(url_for("settings_page"))
        values["check_hour"] = str(_parse_hour(values["check_hour"]))
        saved_now = db.get_settings()
        for k in ("marketplace", "language", "smtp_security",
                  "price_threshold"):
            if k not in request.form:  # not on the form: keep what's saved
                values[k] = saved_now.get(k, "")
        if values["price_threshold"]:
            amount = wishlist.parse_amount(values["price_threshold"])
            if amount is None:
                flash("Wishlist alert price must be an amount such as 5 or "
                      "4.99 — settings not saved", "error")
                return redirect(url_for("settings_page"))
            values["price_threshold"] = f"{amount:.2f}"
        if (values["marketplace"] not in audible_api.MARKETPLACES
                or values["language"] not in ("", *audible_api.LANGUAGES)
                or values["smtp_security"] not in ("ssl", "starttls", "none")):
            flash("Unknown marketplace, language or security setting — "
                  "settings not saved", "error")
            return redirect(url_for("settings_page"))
        port = values["smtp_port"]
        if port and not (port.isdigit() and 1 <= int(port) <= 65535):
            flash("SMTP port must be a number such as 465 or 587 — "
                  "settings not saved", "error")
            return redirect(url_for("settings_page"))
        current = db.get_settings()
        # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces
        # aren't part of it
        pw = "".join(request.form.get("smtp_password", "").split())
        if pw:  # blank = keep existing...
            values["smtp_password"] = pw
        elif (values["smtp_host"] != current.get("smtp_host")
              or values["smtp_user"] != current.get("smtp_user")):
            # ...but only for the server/account it was entered for, so the
            # saved app password can never be redirected to another server
            values["smtp_password"] = ""
            if current.get("smtp_password"):
                flash("Mail server or username changed — enter the app "
                      "password again", "error")
        values["email_enabled"] = "1" if request.form.get("email_enabled") else "0"
        values["lookup_publish_dates"] = (
            "1" if request.form.get("lookup_publish_dates") else "0")
        marketplace_changed = (values["marketplace"]
                               != current.get("marketplace", "us"))
        threshold_changed = (values["price_threshold"]
                             != current.get("price_threshold", ""))
        db.save_settings(values)
        if threshold_changed:
            db.rearm_all_price_alerts()
        if marketplace_changed:
            db.reset_for_marketplace_change()
        reschedule()
        flash("Settings saved", "ok")
        if marketplace_changed:
            flash("Marketplace changed. The next check re-records every "
                  "watch for the new store; nothing will be flagged as "
                  "new.", "ok")
        saved = db.get_settings()
        if values["email_enabled"] == "1" and not checker.email_ready(saved):
            flash("Email is switched on, but the email address or app "
                  "password is missing, so nothing can be sent yet. See "
                  "the setup steps under Email notifications.", "error")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", settings=db.get_settings(),
                           email_ready=checker.email_ready(db.get_settings()),
                           access=access_info(), **_account_context(),
                           marketplaces=sorted(audible_api.MARKETPLACES),
                           languages=audible_api.LANGUAGES)


# ------------------------------------------------------------------ actions

@app.route("/watch/add", methods=["POST"])
def add_watch():
    # forms with class js-watch submit via fetch() and expect JSON back,
    # so the page the user is on never reloads
    wants_json = request.headers.get("X-Requested-With") == "fetch"
    wtype = request.form.get("type", "author")
    name = (request.form.get("name") or "").strip()
    asin = request.form.get("asin") or None
    if wtype not in ("author", "series"):
        if wants_json:
            return {"ok": False, "error": "Unknown watch type"}, 400
        flash("Unknown watch type", "error")
        return redirect(request.referrer or url_for("dashboard"))
    if not name:
        if wants_json:
            return {"ok": False, "error": "Name is required"}, 400
        flash("Name is required", "error")
        return redirect(request.referrer or url_for("dashboard"))

    existing_id = db.find_watch(wtype, name)
    if existing_id is not None:
        # Re-adding must not re-record the baseline: a release that
        # appeared since the last check would be filed as old and never
        # flagged or emailed.
        msg = f"You're already watching {wtype} “{name}”."
        if wants_json:
            return {"ok": True, "message": msg,
                    "watch_url": url_for("watch_detail",
                                         watch_id=existing_id)}
        flash(msg, "ok")
        return redirect(url_for("watch_detail", watch_id=existing_id))

    notes = []
    watch_id = db.add_watch(wtype, name, asin)
    try:
        n = checker.baseline_watch(watch_id)
        if n == 0 and wtype == "author":
            notes.append((f"Now watching author “{name}”, but Audible lists "
                          "no titles under exactly that name. Check the "
                          "spelling, or add them from a search result.",
                          "error"))
        else:
            notes.append((f"Now watching {wtype} “{name}” — found {n} "
                          "existing title(s). You'll be notified about "
                          "anything newer.", "ok"))
    except Exception as e:  # noqa: BLE001
        notes.append((f"Added “{name}” but the initial fetch failed: {e}",
                      "error"))

    # watching a series implies watching its author for anything else they
    # write. If the series' fetch failed, the first check that reaches
    # Audible adds the author instead.
    if wtype == "series":
        added = checker.ensure_series_author(watch_id)
        if added:
            author, n = added
            notes.append((f"Also watching author “{author}” — "
                          f"found {n} existing title(s).", "ok"))

    if wants_json:
        return {"ok": True,
                "watch_url": url_for("watch_detail", watch_id=watch_id),
                "message": " ".join(m for m, _ in notes)}
    for msg, cat in notes:
        flash(msg, cat)
    return redirect(url_for("dashboard"))


@app.route("/watch/<int:watch_id>/delete", methods=["POST"])
def delete_watch(watch_id):
    db.remove_watch(watch_id)
    flash("Watch removed", "ok")
    return redirect(url_for("dashboard"))


@app.route("/check-now", methods=["POST"])
def check_now():
    """Start a check in the background: with many watches it can take
    minutes, longer than a browser or proxy will wait for a page. The
    dashboard refreshes itself until it's done; results land in Recent
    checks."""
    global _check_thread
    if not _check_lock.acquire(blocking=False):
        flash("A check is already running", "error")
        return redirect(url_for("dashboard"))
    _check_thread = threading.Thread(target=_run_check, args=("manual",),
                                     daemon=True)
    _check_thread.start()
    flash("Checking Audible now. This page refreshes by itself; results "
          "appear under Recent checks.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/book/<asin>/<int:watch_id>/seen", methods=["POST"])
def book_seen(asin, watch_id):
    db.mark_seen(asin)
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/book/<asin>/<int:watch_id>/hide", methods=["POST"])
def book_hide(asin, watch_id):
    db.set_book_hidden(asin, watch_id, True)
    return redirect(request.referrer or url_for("watch_detail",
                                                watch_id=watch_id))


@app.route("/book/<asin>/<int:watch_id>/unhide", methods=["POST"])
def book_unhide(asin, watch_id):
    db.set_book_hidden(asin, watch_id, False)
    return redirect(request.referrer or url_for("watch_detail",
                                                watch_id=watch_id))


@app.route("/seen-all", methods=["POST"])
def seen_all():
    db.mark_all_seen()
    flash("All new books marked as seen", "ok")
    return redirect(url_for("dashboard"))


@app.route("/wishlist/refresh", methods=["POST"])
def wishlist_refresh():
    """Re-read the wishlist and its prices now (alerts included)."""
    back = url_for("dashboard", tab="wishlist")
    if not account.is_connected():
        flash("Connect your Audible account first (Settings)", "error")
        return redirect(back)
    if not _check_lock.acquire(blocking=False):
        flash("A check is already running — try again in a minute", "error")
        return redirect(back)
    try:
        result = wishlist.check("manual")
    finally:
        _check_lock.release()
    if result["error"]:
        flash(result["error"], "error")
    else:
        msg = f"Wishlist refreshed — {result['items']} title(s)"
        if result["alerts"]:
            msg += (f", {result['alerts']} newly at or below your alert price"
                    + (" (emailed)" if result["email_sent"] else ""))
        flash(msg, "ok")
    return redirect(back)


@app.route("/wishlist/<asin>/target", methods=["POST"])
def wishlist_target(asin):
    back = url_for("dashboard", tab="wishlist")
    raw = (request.form.get("target") or "").strip()
    target = None
    if raw:
        target = wishlist.parse_amount(raw)
        if target is None:
            flash("Alert price must be an amount such as 5 or 4.99", "error")
            return redirect(back)
    if not db.set_wishlist_target(asin, target):
        flash("That title isn't on your wishlist any more", "error")
    elif target is None:
        flash("Alert price reset to your default", "ok")
    else:
        flash(f"Alert price set to {wishlist.format_price(target)}", "ok")
    return redirect(back)


def _account_context() -> dict:
    """What the Audible account section of the Settings page shows."""
    connected = account.is_connected()
    return {"connected": connected,
            "name": account.account_name() if connected else None,
            "login_url": account.pending_login_url(),
            "owned_count": db.owned_count(),
            "synced_at": db.get_settings().get("library_synced_at")}


def _account_section():
    return url_for("settings_page", _anchor="account")


@app.route("/account")
def account_page():
    """The account section now lives on the Settings page."""
    return redirect(_account_section())


@app.route("/account/start", methods=["POST"])
def account_start():
    mp = db.get_settings().get("marketplace", "us")
    try:
        account.start_login(mp)
    except Exception as e:  # noqa: BLE001
        flash(f"Could not start the Amazon login: {e}", "error")
    return redirect(_account_section())


@app.route("/account/complete", methods=["POST"])
def account_complete():
    url = (request.form.get("response_url") or "").strip()
    if not url:
        flash("Paste the full URL of the page Amazon left you on", "error")
        return redirect(_account_section())
    try:
        account.complete_login(url)
        n = account.sync_library()
        flash(f"Audible account connected — {n} owned title(s) synced", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Connecting failed: {e}", "error")
    return redirect(_account_section())


@app.route("/account/sync", methods=["POST"])
def account_sync():
    try:
        n = account.sync_library()
        flash(f"Library synced — {n} owned title(s)", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Library sync failed: {e}", "error")
    return redirect(_account_section())


@app.route("/account/disconnect", methods=["POST"])
def account_disconnect():
    if account.disconnect():
        flash("Audible account disconnected: this app's device was removed "
              "from your Amazon account and the owned-library data deleted.",
              "ok")
    else:
        flash("Disconnected and owned-library data deleted, but Amazon "
              "couldn't be told. Remove the “Audible Watcher” device "
              "yourself under Amazon's “Manage Your Content and "
              "Devices”.", "error")
    return redirect(_account_section())


@app.route("/healthz")
def healthz():
    """Liveness check for Docker's HEALTHCHECK. Reveals nothing, so it
    needs no sign-in."""
    return {"app": "audible-watcher", "ok": True}


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _password():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        who = f"ip:{request.remote_addr}"
        wait = _lockout_left(who)
        if wait:
            error = (f"Too many wrong passwords. Wait {wait} seconds, then "
                     "try again.")
            return render_template("login.html", error=error,
                                   next=request.values.get("next", "")), 429
        typed = request.form.get("password", "").encode()
        if secrets.compare_digest(typed, _password().encode()):
            _clear_failures(who)
            session["signed_in"] = _password_tag()
            session.permanent = True  # stay signed in for 30 days
            return redirect(_safe_next(request.form.get("next")))
        log.warning("Failed sign-in from %s", request.remote_addr)
        _record_failure(who)
        error = "That password isn't right."
    # keep where the user was heading across a wrong attempt
    return render_template("login.html", error=error,
                           next=request.values.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("signed_in", None)
    return redirect(url_for("login"))


@app.route("/test-email", methods=["POST"])
def test_email():
    try:
        checker.send_test_email(db.get_settings())
        flash("Test email sent — check your inbox", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Test email failed: {checker.explain_smtp_error(e)}",
              "error")
    return redirect(url_for("settings_page"))


HOST = os.environ.get("AW_HOST", "127.0.0.1")  # 0.0.0.0 = reachable on the LAN
PORT = int(os.environ.get("AW_PORT", "5055"))


def start_scheduler():
    """Start the daily check. Call once, before serving."""
    reschedule()
    scheduler.start()


if __name__ == "__main__":
    start_scheduler()
    try:  # production server (installed from requirements.txt)
        from waitress import serve
        log.info("Audible Watcher on http://%s:%s", HOST, PORT)
        serve(app, host=HOST, port=PORT, threads=8)
    except ImportError:
        app.run(host=HOST, port=PORT, debug=False)
