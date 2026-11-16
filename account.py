"""Audible account connection for owned-library sync, via the `audible`
package's external-browser login.

You sign in on amazon.com in your own browser — the app never sees your
credentials. It stores only the resulting device tokens (audible_auth.json
next to the database), which you can revoke at any time from Amazon's
"Manage Your Content and Devices" page.
"""
from __future__ import annotations

import logging
import os
import threading

import audible

import audible_api
import db
from paths import DATA_DIR

log = logging.getLogger(__name__)

AUTH_FILE = os.path.join(DATA_DIR, "audible_auth.json")

# our marketplace codes happen to match the audible package's locales
LOCALES = {"us", "uk", "ca", "au", "de", "fr", "in", "it", "es", "jp"}
ORDER_PAGES = 20  # 50 orders a page, newest first; pre-orders are recent
WISHLIST_PAGE_SIZE = 50
WISHLIST_PAGES = 40  # 2,000 titles: far beyond any real wishlist
# the response groups the wishlist endpoint is documented to accept; an
# unknown group fails the whole request
WISHLIST_GROUPS = "contributors,media,price,product_attrs,product_desc"


def _save_auth(auth):
    """Write the device tokens, readable only by this user from the moment
    the file exists, and atomically: written to a private temp file, then
    swapped in, so a crash mid-write can't leave a half-written token file.
    (Not encrypted: the key would have to sit right next to the file.)"""
    tmp = AUTH_FILE + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(fd)
    try:
        auth.to_file(tmp, encryption=False, set_default=False)
        os.replace(tmp, AUTH_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class _PendingLogin:
    """State shared between the web routes and the login worker thread.

    Authenticator.from_login_external drives the whole login in one call and
    asks for the pasted response URL via a callback, so the flow runs in a
    thread that blocks between the two web requests (start / complete).
    """

    def __init__(self):
        self.login_url: str | None = None
        self.url_ready = threading.Event()
        self.response_url: str | None = None
        self.response_ready = threading.Event()
        self.done = threading.Event()
        self.error: str | None = None


_pending: _PendingLogin | None = None


def is_connected() -> bool:
    return os.path.exists(AUTH_FILE)


def pending_login_url() -> str | None:
    return _pending.login_url if _pending else None


def start_login(marketplace: str) -> str:
    """Begin the external-browser login; returns the amazon.com sign-in URL."""
    global _pending
    locale = marketplace if marketplace in LOCALES else "us"
    p = _PendingLogin()

    def url_callback(url: str) -> str:
        p.login_url = url
        p.url_ready.set()
        if not p.response_ready.wait(timeout=900):
            raise TimeoutError("login was not completed within 15 minutes")
        return p.response_url

    def run():
        try:
            auth = audible.Authenticator.from_login_external(
                locale=locale, login_url_callback=url_callback)
            _save_auth(auth)
        except Exception as e:  # noqa: BLE001 - surfaced via p.error
            log.exception("Audible login failed")
            p.error = str(e)
        finally:
            p.done.set()

    threading.Thread(target=run, daemon=True).start()
    if not p.url_ready.wait(timeout=30):
        raise RuntimeError("could not build the Amazon sign-in URL"
                           + (f" — {p.error}" if p.error else ""))
    _pending = p
    return p.login_url


def complete_login(response_url: str, timeout: int = 90):
    """Hand the pasted post-login URL to the waiting login thread."""
    global _pending
    p = _pending
    if not p:
        raise RuntimeError("no login in progress — click Connect first")
    p.response_url = response_url.strip()
    p.response_ready.set()
    if not p.done.wait(timeout=timeout):
        raise RuntimeError("login did not finish in time — try again")
    _pending = None
    if p.error:
        raise RuntimeError(p.error)


def disconnect() -> bool:
    """Deregister this device with Amazon, then forget the device tokens and
    owned-library data. Returns whether Amazon confirmed the deregistration;
    if not (offline, tokens already revoked), the local data is removed
    anyway and the user can remove the device on Amazon's "Manage Your
    Content and Devices" page."""
    global _pending
    _pending = None
    deregistered = False
    if os.path.exists(AUTH_FILE):
        try:
            audible.Authenticator.from_file(AUTH_FILE).deregister_device()
            deregistered = True
        except Exception:  # noqa: BLE001 - never block a disconnect
            log.warning("Could not deregister the device with Amazon",
                        exc_info=True)
        os.remove(AUTH_FILE)
    db.replace_owned([])
    db.replace_wishlist([])
    return deregistered


def account_name() -> str | None:
    if not is_connected():
        return None
    try:
        auth = audible.Authenticator.from_file(AUTH_FILE)
        return (auth.customer_info or {}).get("name")
    except Exception:  # noqa: BLE001
        log.exception("Could not read auth file")
        return None


def _preordered_asins(client) -> list[str]:
    """ASINs of placed-but-undelivered orders (pre-orders). Audible keeps
    pre-orders in the orders feed until release day — they only move into
    the library once they're delivered."""
    asins, page = [], 1
    while page <= ORDER_PAGES:
        data = client.get("1.0/orders", num_results=50, page=page)
        orders = data.get("orders") or []
        if not orders:
            break
        for o in orders:
            if o.get("detail_state") != "ORDER_CREATED":
                continue
            for it in o.get("items") or []:
                asin = (it.get("product") or {}).get("asin") or it.get("asin")
                if asin:
                    asins.append(asin)
        if len(orders) < 50:
            break
        page += 1
    return asins


def sync_library() -> int:
    """Fetch the full owned library (plus pending pre-orders) into the
    database; returns title count."""
    auth = audible.Authenticator.from_file(AUTH_FILE)
    marketplace = db.get_settings().get("marketplace", "us")
    items = []
    orders_failed = False
    try:
        with audible.Client(auth=auth) as client:
            page = 1
            while True:
                data = client.get(
                    "1.0/library", num_results=500, page=page,
                    response_groups="product_desc,product_attrs,"
                                    "contributors,series,media")
                batch = data.get("items") or []
                items.extend(batch)
                if len(batch) < 500:
                    break
                page += 1
            try:
                preordered = _preordered_asins(client)
            except Exception:  # noqa: BLE001 - orders feed is best-effort
                log.exception("Could not read the orders feed for pre-orders;"
                              " keeping the pre-orders from the last sync")
                preordered = []
                orders_failed = True
    finally:
        # the client may have refreshed the access token even if the sync
        # failed part-way; keep it either way
        _save_auth(auth)

    rows = []
    for i in items:
        b = audible_api._simplify(i, marketplace)
        b["purchase_date"] = (i.get("purchase_date") or "")[:10] or None
        rows.append(b)

    # pre-orders aren't in the library yet — pull their details from the
    # public catalog so they show up properly
    in_library = {b["asin"] for b in rows}
    if orders_failed:
        # one Amazon hiccup mustn't wipe them: keep the last known
        # pre-orders until a sync can read the order list again
        for b in db.owned_preorders():
            if b["asin"] not in in_library:
                rows.append(b)
                in_library.add(b["asin"])
    pending = [a for a in preordered if a not in in_library]
    if pending:
        details = {}
        try:
            details = {b["asin"]: b for b in
                       audible_api._fetch_by_asins(pending, marketplace)}
        except audible_api.AudibleAPIError:
            log.warning("Catalog lookup for pre-ordered titles failed")
        for asin in pending:
            b = details.get(asin) or {"asin": asin}
            b["preordered"] = 1
            rows.append(b)

    db.replace_owned(rows)
    db.save_settings({"library_synced_at": db.now_iso()})
    return len(rows)


def fetch_wishlist() -> list[dict]:
    """The account's whole Audible wishlist as raw catalog products, with
    prices. Raises if Amazon can't be asked, so a failed fetch never looks
    like an empty wishlist.

    Paging: stops at a short page, an empty page, total_results, or two
    pages in a row that add nothing new. The last rule makes it safe
    whether this endpoint counts pages from 0 or from 1 (one repeated
    page is skipped, not mistaken for the end)."""
    auth = audible.Authenticator.from_file(AUTH_FILE)
    products: list[dict] = []
    seen: set[str] = set()
    try:
        with audible.Client(auth=auth) as client:
            idle = 0
            for page in range(WISHLIST_PAGES):
                data = client.get("1.0/wishlist",
                                  num_results=WISHLIST_PAGE_SIZE, page=page,
                                  response_groups=WISHLIST_GROUPS,
                                  sort_by="-DateAdded")
                batch = data.get("products") or data.get("items") or []
                fresh = [p for p in batch
                         if p.get("asin") and p["asin"] not in seen]
                for p in fresh:
                    seen.add(p["asin"])
                products.extend(fresh)
                total = data.get("total_results")
                if (not batch or len(batch) < WISHLIST_PAGE_SIZE
                        or (isinstance(total, int) and len(seen) >= total)):
                    break
                idle = 0 if fresh else idle + 1
                if idle >= 2:
                    break
    finally:
        _save_auth(auth)  # keep a refreshed access token either way
    return products
