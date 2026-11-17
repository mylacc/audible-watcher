"""Release checking and email notification logic."""
from __future__ import annotations

import html
import logging
import smtplib
import socket
import ssl
import threading
import time
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parseaddr

import audible_api
import bookinfo
import db

log = logging.getLogger(__name__)


_enrich_lock = threading.Lock()


def background_enrichment_async():
    """Run the metadata passes (marketplace availability, publish dates) in
    the background so adding a watch or running a check never blocks on
    them. Skips silently if a batch is already running."""
    threading.Thread(target=run_enrichment, daemon=True).start()


def run_enrichment():
    """One batch of metadata passes, unless one is already running."""
    if not _enrich_lock.acquire(blocking=False):
        return
    try:
        verify_market_availability()
        # publish dates come from Google Books / Open Library: only if the
        # user hasn't switched those lookups off
        if db.get_settings().get("lookup_publish_dates", "1") == "1":
            enrich_published_dates()
    finally:
        _enrich_lock.release()


def verify_market_availability(limit: int = 50):
    """Check not-yet-validated books against the marketplace's own store
    listing and hide editions that aren't sold there (series relationships
    include foreign-region editions like UK printings)."""
    marketplace = db.get_settings().get("marketplace", "us")
    for r in db.books_missing_availability(limit):
        author = audible_api.first_author(r["authors"])
        try:
            ok = audible_api.check_market_availability(
                r["asin"], r["title"], author, marketplace)
        except audible_api.AudibleAPIError:
            log.info("Availability checks unavailable — deferring the rest")
            break
        if ok is False:
            db.set_availability(r["asin"], False)
            db.hide_asin_everywhere(r["asin"])
            log.info("Hidden %r (%s) — not sold on this marketplace",
                     r["title"], r["asin"])
        else:
            # sold here, or the search couldn't say (None): no conclusion
            # never hides a book, so it stays as it is — visible
            db.set_availability(r["asin"], True)
        time.sleep(0.3)  # be polite


def enrich_published_dates(limit: int = 20):
    """Look up original (print/ebook) publish dates for books that don't have
    one yet — a bounded, throttled batch per run so checks stay fast and the
    sources aren't hammered."""
    for r in db.books_missing_published(limit):
        author = audible_api.first_author(r["authors"])
        try:
            pd, definitive = bookinfo.published_date(r["title"], author)
        except bookinfo.RateLimited:
            log.info("Publish-date sources unavailable — deferring the rest "
                     "of this batch to the next check")
            break
        except Exception:  # noqa: BLE001 - enrichment must never break checks
            log.exception("Publish-date lookup failed for %r", r["title"])
            break
        if pd or definitive:
            db.set_published_date(r["asin"], pd or "")
        time.sleep(0.5)  # be polite to free APIs


def _split_by_language(books: list[dict], settings: dict) -> tuple[list, list]:
    """Partition fetched books into (kept, foreign) per the language setting."""
    language = (settings.get("language") or "").strip().lower()
    keep, foreign = [], []
    for b in books:
        (keep if audible_api.matches_language(b, language) else foreign).append(b)
    return keep, foreign


def _store_foreign(watch_id: int, foreign: list[dict]):
    """Record wrong-language editions as hidden: invisible, never alerted,
    but remembered so checks don't re-flag them as new releases."""
    if foreign:
        db.upsert_books(watch_id, foreign, as_new=False, hidden=True)
        db.hide_books(watch_id, [b["asin"] for b in foreign])


def _sold_here(pending: list[dict], marketplace: str) -> list[dict]:
    """The pending rows that are safe to email: books confirmed as sold in
    this marketplace. Series relationships include other regions' editions,
    so an unverified book is checked now, before anything is sent.

    Held back for a later check (left unnotified): books with no release
    date yet (absent from the store search proves nothing until they have
    one) and books whose lookup failed. Editions not sold here are hidden
    everywhere and never emailed.
    """
    verdict: dict[str, bool] = {}
    api_down = False
    for b in pending:
        asin = b["asin"]
        if asin in verdict:
            continue
        if b.get("available") is not None:
            verdict[asin] = bool(b["available"])
        elif not b.get("release_date") or api_down:
            verdict[asin] = False  # hold until it can be verified
        else:
            author = audible_api.first_author(b.get("authors"))
            try:
                ok = audible_api.check_market_availability(
                    asin, b["title"], author, marketplace)
            except audible_api.AudibleAPIError:
                log.info("Availability check failed — holding new-book "
                         "emails until the next check")
                api_down = True
                verdict[asin] = False
                continue
            if ok is False:
                db.set_availability(asin, False)
                db.hide_asin_everywhere(asin)
                log.info("Not emailing %r (%s) — not sold on this "
                         "marketplace", b["title"], asin)
            else:  # present, or the search couldn't say: keep it
                db.set_availability(asin, True)
            verdict[asin] = ok is not False
            time.sleep(0.3)  # be polite
    held = {a for a, ok in verdict.items() if not ok}
    if held:
        log.info("Holding %d new book(s) back from email until verified",
                 len(held))
    return [b for b in pending if verdict[b["asin"]]]


def recheck_not_sold_here(marketplace: str, limit: int = 20) -> int:
    """Ask the store again about books hidden as "not sold here" (each about
    once a week, a small batch per check). One that's sold here now comes
    back as a new book, so it's flagged and emailed. Returns how many."""
    restored = 0
    for r in db.books_to_recheck(limit):
        try:
            ok = audible_api.check_market_availability(
                r["asin"], r["title"], audible_api.first_author(r["authors"]),
                marketplace)
        except audible_api.AudibleAPIError:
            log.info("Store re-check unavailable — trying again next check")
            break
        # ok is None when the store search returns nothing at all: no
        # conclusion, so the book keeps its current state (here: hidden)
        # and is asked about again next week. The first check follows the
        # same rule from the other side: no conclusion keeps a book visible.
        if ok:
            n = db.restore_now_available(r["asin"])
            restored += 1
            log.info("%r (%s) is sold in this store now — back on %d "
                     "watch(es) as new", r["title"], r["asin"], n)
        else:
            db.mark_availability_checked(r["asin"])
        time.sleep(0.3)  # be polite
    return restored


def _empty_note(watch: dict, books: list) -> str | None:
    """A warning for the watch list when a watch finds nothing: usually an
    author name that doesn't exactly match how Audible credits them, or a
    series your Audible store doesn't carry."""
    if books:
        return None
    if watch["type"] == "author":
        return ("Audible lists no titles credited to exactly this name. "
                "Check the spelling, or re-add the author from a search.")
    return ("Audible found no books for this series in your store. If you "
            "changed marketplace, try removing it and adding it again from "
            "a search.")


MAX_UNREACHABLE = 3  # watches in a row Audible can't be reached for
RECENT_DAYS = 30     # see _split_recent
BULK_EMAIL_LIMIT = 10  # new books from one watch listed one by one in email


def _split_recent(books: list[dict], last_checked: str | None):
    """(recent, older) for a watch's first check after an app update that
    changed what's fetched. Older titles only showed up because the app
    now looks further; recent ones (released since about the last check,
    upcoming, or undated) may be real new releases and still alert."""
    ref = (last_checked or "")[:10] or date.today().isoformat()
    try:
        since = (date.fromisoformat(ref)
                 - timedelta(days=RECENT_DAYS)).isoformat()
    except ValueError:
        since = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    recent = [b for b in books
              if not b.get("release_date") or b["release_date"] >= since]
    older = [b for b in books
             if b.get("release_date") and b["release_date"] < since]
    return recent, older


def _update_watch(w: dict, settings: dict) -> list[dict]:
    """Fetch one watch and store its books; returns the newly found ones.

    Until a watch's first fetch has succeeded (it failed when the watch was
    added, or the marketplace changed), what's on Audible is recorded as its
    baseline — already seen, never emailed — instead of all of it being
    flagged as new releases.
    """
    marketplace = settings.get("marketplace", "us")
    first_fetch = not w.get("baselined_at")
    fetched = audible_api.fetch_watch_books(
        w["type"], w["name"], w["asin"], marketplace)
    podcasts = {b["asin"] for b in fetched if audible_api.is_podcast(b)}
    books = [b for b in fetched if not audible_api.is_podcast(b)]
    if first_fetch and w["type"] == "series" and not books:
        # Each Audible store has its own series IDs, so after a marketplace
        # change the stored one finds nothing: look the series up in this
        # store. Only then — an ID that still works is never second-guessed
        # (another series may share the name).
        asin = _resolve_same_series(w, marketplace)
        if asin and asin != w.get("asin"):
            log.info("Series %r is %s in this store", w["name"], asin)
            db.set_watch_asin(w["id"], asin)
            w = dict(w, asin=asin)
            books = [b for b in audible_api.fetch_watch_books(
                w["type"], w["name"], w["asin"], marketplace)
                if not audible_api.is_podcast(b)]
    keep, foreign = _split_by_language(books, settings)
    _store_foreign(w["id"], foreign)
    updated_fetch = (not first_fetch and (w.get("fetch_rev") or 0)
                     < audible_api.FETCH_REVISION)
    if first_fetch:
        fresh = db.upsert_books(w["id"], keep, as_new=False)
    elif updated_fetch:
        recent, older = _split_recent(keep, w.get("last_checked_at"))
        quiet = db.upsert_books(w["id"], older, as_new=False)
        fresh = db.upsert_books(w["id"], recent, as_new=True)
        if quiet:
            log.info("Watch %r: %d older title(s) found by the updated "
                     "fetch were recorded quietly", w["name"], len(quiet))
    else:
        fresh = db.upsert_books(w["id"], keep, as_new=True)
    db.set_fetch_rev(w["id"], audible_api.FETCH_REVISION)
    db.hide_podcasts(w["id"], podcasts)  # podcasts linked before 1.0.6
    if w["type"] == "author":
        gone = db.hide_not_by_author(w["id"], w["name"],
                                     {b["asin"] for b in books})
        if gone:
            log.info("Watch %r: hid %d title(s) that aren't by this author "
                     "or are podcasts", w["name"], gone)
    stubs = db.hide_placeholders(w["id"])
    if stubs:
        log.info("Watch %r: hid %d no-date placeholder(s) of dated books",
                 w["name"], stubs)
    fresh = [b for b in fresh if b["asin"] not in
             {x["asin"] for x in db.books_for_watch(w["id"],
                                                    include_hidden=True)
              if x["placeholder_hidden"]}]
    if first_fetch:
        db.mark_baselined(w["id"])
        if books:  # leave nothing from the previous store on the page
            db.hide_other_store_links(w["id"],
                                      audible_api.store_prefix(marketplace))
        if w["type"] == "series" and not w.get("book_count"):
            # added while Audible was unreachable: its author watch couldn't
            # be added then. (Not after a marketplace change: an author
            # watch you removed on purpose stays removed.)
            ensure_series_author(w["id"])
    db.mark_watch_checked(w["id"], _empty_note(w, books))
    return [] if first_fetch else fresh


def _resolve_same_series(w: dict, marketplace: str) -> str | None:
    """This store's ID for the watched series: a series with exactly this
    name whose authors match the books already on the watch. With no books
    to compare, only an unambiguous name counts; if several series share
    the name, nothing is guessed."""
    try:
        candidates = [
            c for c in audible_api.search_series_candidates(w["name"],
                                                            marketplace)
            if (c["series_title"] or "").strip().casefold()
            == w["name"].strip().casefold()]
    except audible_api.AudibleUnavailable:
        raise
    except audible_api.AudibleAPIError:
        return None
    known = {audible_api.norm_name(a)
             for b in db.books_for_watch(w["id"], include_hidden=True)
             for a in audible_api.split_authors(b["authors"])}
    if known:
        matching = [c for c in candidates
                    if known & {audible_api.norm_name(a) for a in
                                audible_api.split_authors(c["sample_authors"])}]
        return matching[0]["series_asin"] if len(matching) == 1 else None
    return candidates[0]["series_asin"] if len(candidates) == 1 else None


def _primary_author(watch_id: int) -> str | None:
    """Most common author name among a watch's visible books."""
    counts: dict[str, int] = {}
    for b in db.books_for_watch(watch_id):
        for name in audible_api.split_authors(b["authors"]):
            counts[name] = counts.get(name, 0) + 1
    return max(counts, key=counts.get) if counts else None


def ensure_series_author(series_watch_id: int) -> tuple[str, int] | None:
    """Watching a series also watches its main author. Returns (author,
    books found) when a new author watch was added, else None. Runs when
    the series is added, and again on its first successful fetch in case
    Audible was unreachable when it was added."""
    author = _primary_author(series_watch_id)
    if not author:
        return None
    if db.find_watch("author", author) is not None:
        return None
    author_id = db.add_watch("author", author, None)
    try:
        return author, baseline_watch(author_id)
    except Exception:  # noqa: BLE001 - the next check records it quietly
        # (its baseline is still pending, so nothing gets flagged as new)
        log.info("Added author watch %r; its first fetch is pending", author)
        return author, 0


def check_all(trigger: str = "manual") -> dict:
    """Check every watch for new books, send one digest email if any found.

    If Audible can't be reached for several watches in a row, the rest are
    skipped (each would only time out too) and the result says so with
    aborted=True, so the caller can retry later.
    """
    settings = db.get_settings()
    marketplace = settings.get("marketplace", "us")
    total_new = 0
    errors = []
    unreachable = 0
    aborted = False

    watches = db.list_watches()
    for i, w in enumerate(watches):
        try:
            fresh = _update_watch(w, settings)
            unreachable = 0
            total_new += len(fresh)
            if fresh:
                log.info("Watch %r: %d new book(s)", w["name"], len(fresh))
        except Exception as e:  # noqa: BLE001 - keep checking other watches
            if db.get_watch(w["id"]) is None:
                log.info("Watch %r was removed during the check", w["name"])
                continue  # deleted while we were fetching: not an error
            log.exception("Check failed for watch %r", w["name"])
            errors.append(f"{w['name']}: {e}")
            db.mark_watch_checked(w["id"], str(e))
            if isinstance(e, audible_api.AudibleUnavailable):
                unreachable += 1
                # 3 in a row (or every watch, if there are fewer): Audible
                # is down, so stop and let the caller retry later
                if unreachable >= min(MAX_UNREACHABLE, len(watches)):
                    skipped = len(watches) - i - 1
                    errors.append(
                        "Audible is unreachable"
                        + (f" — skipped the other {skipped} watch(es)"
                           if skipped else "")
                        + "; will try again later")
                    aborted = True
                    break
            else:
                unreachable = 0

    if not aborted:
        recheck_not_sold_here(marketplace)
    background_enrichment_async()

    email_sent = False
    pending = db.unnotified_books()
    if pending and settings.get("email_enabled") != "1":
        # Email is off: these books are on the dashboard already. Don't save
        # them up for one enormous email the day email gets switched on.
        db.mark_notified([(b["asin"], b["watch_id"]) for b in pending])
        pending = []
    seen = [b for b in pending if not b["is_new"]]
    if seen:  # marked seen before an email went out (e.g. SMTP was failing)
        db.mark_notified([(b["asin"], b["watch_id"]) for b in seen])
        pending = [b for b in pending if b["is_new"]]
    if pending:
        ready = _sold_here(pending, marketplace)
        if ready:
            try:
                send_new_release_email(
                    _collapse_bulk(db.dedupe_by_asin(ready)), settings)
                db.mark_notified([(b["asin"], b["watch_id"]) for b in ready])
                email_sent = True
            except Exception as e:  # noqa: BLE001
                log.exception("Email send failed")
                errors.append(f"email: {explain_smtp_error(e)}")

    db.log_check(trigger, total_new, "; ".join(errors) or None, email_sent)
    return {"new_books": total_new, "errors": errors, "email_sent": email_sent,
            "aborted": aborted}


def _collapse_bulk(books: list[dict]) -> list[dict]:
    """Safety net: if one watch has more than BULK_EMAIL_LIMIT new books in
    a single email, list it as one summary line instead (they're all on the
    dashboard). A real author or series rarely releases that many at once;
    a burst usually means Audible re-listed a back catalogue."""
    per_watch: dict = {}
    for b in books:
        per_watch.setdefault((b["watch_type"], b["watch_name"]), []).append(b)
    out, summaries = [], []
    for (wtype, wname), group in per_watch.items():
        if len(group) > BULK_EMAIL_LIMIT:
            summaries.append({"bulk_count": len(group), "watch_name": wname,
                              "watch_type": wtype})
        else:
            out.extend(group)
    return out + summaries


def baseline_watch(watch_id: int) -> int:
    """Initial fetch for a newly added watch; books stored as already-seen."""
    w = db.get_watch(watch_id)
    if not w:
        return 0
    settings = db.get_settings()
    books = [b for b in audible_api.fetch_watch_books(
        w["type"], w["name"], w["asin"], settings.get("marketplace", "us"))
        if not audible_api.is_podcast(b)]
    keep, foreign = _split_by_language(books, settings)
    _store_foreign(watch_id, foreign)
    inserted = db.upsert_books(watch_id, keep, as_new=False)
    db.hide_placeholders(watch_id)
    db.mark_baselined(watch_id)
    db.set_fetch_rev(watch_id, audible_api.FETCH_REVISION)
    db.mark_watch_checked(watch_id, _empty_note(w, books))
    background_enrichment_async()
    return len(inserted)


# ---------- email ----------

def _book_row_html(b: dict) -> str:
    # every value is escaped: titles like "Q&A" or "<Untitled>" must not
    # break the email's HTML
    e = html.escape
    if b.get("bulk_count"):
        return f"""
    <tr>
      <td></td>
      <td style="padding:10px 0">
        <strong>{e(b['watch_name'])}</strong>: {b['bulk_count']} titles
        were added to Audible at once — they're listed on your Audible
        Watcher dashboard.
      </td>
    </tr>"""
    seq = (f" (Book {e(str(b['series_sequence']))})"
           if b.get("series_sequence") else "")
    series = (f"<br><em>{e(b['series_title'])}{seq}</em>"
              if b.get("series_title") else "")
    date = e(b.get("release_date") or "TBA")
    img = (f'<img src="{e(b["image_url"])}" width="80" alt="" '
           'style="border-radius:6px;vertical-align:top">'
           if b.get("image_url") else "")
    return f"""
    <tr>
      <td style="padding:10px 12px 10px 0">{img}</td>
      <td style="padding:10px 0">
        <a href="{e(b['url'] or '')}" style="font-size:16px;font-weight:bold;
           color:#f29d38;text-decoration:none">{e(b['title'] or '')}</a><br>
        by {e(b.get('authors') or 'Unknown')}{series}<br>
        <span style="color:#666">Release date: {date}</span><br>
        <span style="color:#999;font-size:12px">Watched:
        {e(b['watch_name'])} ({e(b['watch_type'])})</span>
      </td>
    </tr>"""


def build_email(books: list[dict]) -> tuple[str, str, str]:
    n = sum(b.get("bulk_count", 1) for b in books)
    subject = (f"Audible Watcher: {n} new book{'s' if n != 1 else ''} "
               "from your favorites")
    rows = "\n".join(_book_row_html(b) for b in books)
    body = f"""<html><body style="font-family:Arial,Helvetica,sans-serif;
      color:#222;max-width:640px">
      <h2 style="color:#f29d38">🎧 New on Audible</h2>
      <p>{n} new title{'s' if n != 1 else ''} matching your watched authors
      and series:</p>
      <table cellspacing="0" cellpadding="0">{rows}</table>
      <p style="color:#999;font-size:12px;margin-top:24px">
        Sent by your Audible Watcher app.</p>
    </body></html>"""
    text_lines = [f"{n} new Audible title(s) from your favorites:", ""]
    for b in books:
        if b.get("bulk_count"):
            text_lines.append(f"- {b['watch_name']}: {b['bulk_count']} titles "
                              "added to Audible at once — see the dashboard")
            continue
        text_lines.append(f"- {b['title']} by {b.get('authors') or 'Unknown'}"
                          f" — releases {b.get('release_date') or 'TBA'}")
        text_lines.append(f"  {b['url']}")
    return subject, body, "\n".join(text_lines)


def send_email(subject: str, html: str, text: str, settings: dict):
    host = settings["smtp_host"]
    security = settings.get("smtp_security") or "none"
    port = int(settings["smtp_port"]
               or {"ssl": 465, "starttls": 587}.get(security, 25))
    user = settings["smtp_user"]
    password = settings["smtp_password"]
    sender = settings["email_from"] or user
    recipients = [r.strip() for r in (settings["email_to"] or user).split(",")
                  if r.strip()]
    if not host:
        raise RuntimeError("Mail relay not set up: enter its address "
                           "(see Settings page)")
    if bool(user) != bool(password):
        raise RuntimeError("Relay login half filled in: enter both the "
                           "username and the password, or leave both blank "
                           "(see Settings page)")
    if not user and not (sender and recipients):
        raise RuntimeError("Mail relay not set up: the From address and "
                           "“Send notifications to” are both needed "
                           "(see Settings page)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    # domain of the address itself: From may be "Name <me@example.com>"
    address = parseaddr(sender)[1]
    msg["Message-ID"] = make_msgid(domain=address.rpartition("@")[2] or None)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    if security in ("starttls", "none"):
        # "none": plain SMTP, for a mail relay on your own network
        with smtplib.SMTP(host, port, timeout=30) as s:
            if security == "starttls":
                s.starttls(context=ssl.create_default_context())
            if user:
                s.login(user, password)
            s.sendmail(sender, recipients, msg.as_string())
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30,
                              context=ssl.create_default_context()) as s:
            if user:
                s.login(user, password)
            s.sendmail(sender, recipients, msg.as_string())


def explain_smtp_error(e: Exception) -> str:
    """The error plus a plain-language hint about what to fix in Settings."""
    detail = str(e) or type(e).__name__
    if isinstance(e, smtplib.SMTPAuthenticationError):
        hint = ("the mail relay rejected the username or password. Relays "
                "on your own network usually need no login: leave both "
                "blank unless yours asks for one")
    elif isinstance(e, ssl.SSLCertVerificationError):
        hint = ("the mail relay's security certificate couldn't be "
                "verified. Check its name is exactly right, or set "
                "Security to None for a plain relay on your own network")
    elif isinstance(e, ssl.SSLError):
        hint = ("the security setting doesn't match the relay. A plain "
                "relay on your own network needs Security set to None")
    elif isinstance(e, socket.gaierror):
        hint = ("the mail relay's name wasn't found. Check it for typos, "
                "or use its IP address")
    elif isinstance(e, (TimeoutError, socket.timeout)):
        hint = ("the mail relay didn't answer. Check its address, port and "
                "security setting")
    elif isinstance(e, (ConnectionError, smtplib.SMTPConnectError,
                        smtplib.SMTPServerDisconnected)):
        hint = ("couldn't connect to the mail relay. Check its address and "
                "port; from a container, use the NAS's LAN IP, not "
                "localhost")
    elif isinstance(e, smtplib.SMTPSenderRefused):
        hint = ("the relay refused the From address. Use a sender on a "
                "domain the relay accepts")
    elif isinstance(e, smtplib.SMTPRecipientsRefused):
        hint = ("the relay refused the “Send notifications to” address, or "
                "doesn't accept mail from this container's network")
    else:
        return detail
    return f"{hint} ({detail})"


def email_ready(settings: dict) -> bool:
    """Enough is filled in to send: a relay, a From and a To address (or,
    for a relay that needs a login, a username and password)."""
    if not settings.get("smtp_host"):
        return False
    if settings.get("smtp_user") or settings.get("smtp_password"):
        return bool(settings.get("smtp_user") and settings.get("smtp_password"))
    return bool(settings.get("email_from") and settings.get("email_to"))


def send_new_release_email(books: list[dict], settings: dict):
    subject, html, text = build_email(books)
    send_email(subject, html, text, settings)


def send_test_email(settings: dict):
    send_email(
        "Audible Watcher: test email",
        "<p>✅ Your Audible Watcher email settings work.</p>",
        "Your Audible Watcher email settings work.",
        settings,
    )
