"""SQLite storage for Audible Watcher.

Data model: `books` holds one row per catalog entry (shared metadata such as
title, release date, language, publish date); `watch_books` links a book to
each watch that surfaced it and carries the per-watch state (new/seen,
notified, hidden). `owned_books` is a separate snapshot of the connected
Audible account's library.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from paths import DATA_DIR

DB_PATH = os.environ.get("AUDIBLE_WATCHER_DB",
                         os.path.join(DATA_DIR, "watcher.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS watches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL CHECK (type IN ('author', 'series')),
    name            TEXT NOT NULL,
    asin            TEXT,
    created_at      TEXT NOT NULL,
    last_checked_at TEXT,
    last_error      TEXT,
    baselined_at    TEXT,  -- first successful fetch; NULL = not done yet
    fetch_rev       INTEGER NOT NULL DEFAULT 0,  -- see FETCH_REVISION
    UNIQUE (type, name)
);

CREATE TABLE IF NOT EXISTS books (
    asin            TEXT PRIMARY KEY,
    title           TEXT,
    subtitle        TEXT,
    authors         TEXT,
    narrators       TEXT,
    series_title    TEXT,
    series_sequence TEXT,
    all_series      TEXT,     -- JSON list of every series the book is in
    release_date    TEXT,
    runtime_minutes INTEGER,
    language        TEXT,
    published_date  TEXT,
    available       INTEGER,  -- sold in our marketplace? NULL = unchecked
    available_checked_at TEXT,  -- when that was last asked
    image_url       TEXT,
    url             TEXT
);

CREATE TABLE IF NOT EXISTS watch_books (
    watch_id      INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    asin          TEXT NOT NULL REFERENCES books(asin),
    discovered_at TEXT NOT NULL,
    is_new        INTEGER NOT NULL DEFAULT 0,
    notified      INTEGER NOT NULL DEFAULT 0,
    hidden        INTEGER NOT NULL DEFAULT 0,
    store_hidden  INTEGER NOT NULL DEFAULT 0,  -- hidden as another store's
                                               -- edition (marketplace change)
    region_hidden INTEGER NOT NULL DEFAULT 0,  -- hidden as not sold in this
                                               -- store; re-checked weekly
    placeholder_hidden INTEGER NOT NULL DEFAULT 0,  -- no-date stub of a
                                               -- dated listing
    keep_visible  INTEGER NOT NULL DEFAULT 0,  -- you unhid it: never hide
                                               -- it again automatically
    mismatch_hidden INTEGER NOT NULL DEFAULT 0,  -- not this author's book
                                               -- (or a podcast)
    PRIMARY KEY (asin, watch_id)
);

CREATE TABLE IF NOT EXISTS owned_books (
    asin            TEXT PRIMARY KEY,
    title           TEXT,
    authors         TEXT,
    narrators       TEXT,
    series_title    TEXT,
    series_sequence TEXT,
    series_asin     TEXT,
    release_date    TEXT,
    runtime_minutes INTEGER,
    language        TEXT,
    image_url       TEXT,
    url             TEXT,
    purchase_date   TEXT,
    preordered      INTEGER NOT NULL DEFAULT 0
);

-- the connected account's Audible wishlist, with prices (price alerts)
CREATE TABLE IF NOT EXISTS wishlist (
    asin            TEXT PRIMARY KEY,
    title           TEXT,
    authors         TEXT,
    narrators       TEXT,
    series_title    TEXT,
    series_sequence TEXT,
    release_date    TEXT,
    image_url       TEXT,
    url             TEXT,
    price           REAL,     -- what you'd pay now (Audible's lowest price)
    list_price      REAL,
    price_type      TEXT,     -- Audible's label for that price: sale, member...
    currency        TEXT,
    lowest_seen     REAL,     -- lowest price seen since it was added here
    target_price    REAL,     -- per-title alert price; NULL = the default
    alerted_price   REAL,     -- price last alerted at; NULL = not alerted
                              -- since it was last above its target
    position        INTEGER,  -- order on the Audible wishlist
    added_at        TEXT NOT NULL,
    updated_at      TEXT
);

-- internal secrets, kept apart from user-facing settings
CREATE TABLE IF NOT EXISTS app_secrets (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at      TEXT NOT NULL,
    trigger     TEXT,
    new_books   INTEGER DEFAULT 0,
    errors      TEXT,
    email_sent  INTEGER DEFAULT 0
);
"""

DEFAULT_SETTINGS = {
    "marketplace": "us",
    "check_hour": "8",           # local hour of daily check
    "smtp_host": "smtp.gmail.com",
    "smtp_port": "465",
    "smtp_security": "ssl",      # ssl | starttls
    "smtp_user": "",
    "smtp_password": "",
    "email_from": "",
    "email_to": "",
    "email_enabled": "0",
    "language": "",              # '' = any language
    "lookup_publish_dates": "1",  # ask Google Books / Open Library
    "price_threshold": "",       # wishlist alert price; '' = no default
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _rename_legacy_books(c) -> bool:
    """v1 stored one wide row per (book, watch). Detect that layout and move
    it aside so SCHEMA can create the split tables; init_db copies the data
    over afterwards."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(books)")}
    if "watch_id" not in cols:
        return False
    # very old DBs may predate these columns; add them so the copy works
    for col, decl in [("hidden", "INTEGER NOT NULL DEFAULT 0"),
                      ("language", "TEXT"), ("published_date", "TEXT")]:
        if col not in cols:
            c.execute(f"ALTER TABLE books ADD COLUMN {col} {decl}")
    c.execute("ALTER TABLE books RENAME TO books_v1")
    return True


def init_db():
    with conn() as c:
        legacy = _rename_legacy_books(c)
        c.executescript(SCHEMA)
        if legacy:
            c.execute("""
                INSERT OR IGNORE INTO books
                (asin, title, subtitle, authors, narrators, series_title,
                 series_sequence, release_date, runtime_minutes, language,
                 published_date, image_url, url)
                SELECT asin, title, subtitle, authors, narrators,
                       series_title, series_sequence, release_date,
                       runtime_minutes, language, published_date, image_url,
                       url
                FROM books_v1""")
            c.execute("""
                INSERT OR IGNORE INTO watch_books
                (watch_id, asin, discovered_at, is_new, notified, hidden)
                SELECT watch_id, asin, discovered_at, is_new, notified,
                       hidden
                FROM books_v1""")
            c.execute("DROP TABLE books_v1")
        book_cols = {r["name"] for r in c.execute("PRAGMA table_info(books)")}
        if "available" not in book_cols:
            c.execute("ALTER TABLE books ADD COLUMN available INTEGER")
        if "all_series" not in book_cols:
            c.execute("ALTER TABLE books ADD COLUMN all_series TEXT")
        watch_cols = {r["name"]
                      for r in c.execute("PRAGMA table_info(watches)")}
        link_cols = {r["name"]
                     for r in c.execute("PRAGMA table_info(watch_books)")}
        if "store_hidden" not in link_cols:
            c.execute("ALTER TABLE watch_books ADD COLUMN store_hidden "
                      "INTEGER NOT NULL DEFAULT 0")
        for col in ("placeholder_hidden", "keep_visible",
                    "mismatch_hidden"):
            if col not in link_cols:
                c.execute(f"ALTER TABLE watch_books ADD COLUMN {col} "
                          "INTEGER NOT NULL DEFAULT 0")
        if "region_hidden" not in link_cols:
            c.execute("ALTER TABLE watch_books ADD COLUMN region_hidden "
                      "INTEGER NOT NULL DEFAULT 0")
            # earlier versions hid "not sold here" books without saying why;
            # mark them so the weekly re-check can bring them back
            c.execute("""
                UPDATE watch_books SET region_hidden = 1
                WHERE hidden = 1 AND store_hidden = 0
                  AND asin IN (SELECT asin FROM books WHERE available = 0)""")
        book_cols = {r["name"] for r in c.execute("PRAGMA table_info(books)")}
        if "available_checked_at" not in book_cols:
            c.execute("ALTER TABLE books ADD COLUMN available_checked_at TEXT")
        if "fetch_rev" not in watch_cols:
            c.execute("ALTER TABLE watches ADD COLUMN fetch_rev "
                      "INTEGER NOT NULL DEFAULT 0")
        if "baselined_at" not in watch_cols:
            c.execute("ALTER TABLE watches ADD COLUMN baselined_at TEXT")
            # watches from older versions that already have books were
            # baselined back then
            c.execute("""
                UPDATE watches SET baselined_at =
                    COALESCE(last_checked_at, created_at)
                WHERE EXISTS (SELECT 1 FROM watch_books wb
                              WHERE wb.watch_id = watches.id)""")
        owned_cols = {r["name"]
                      for r in c.execute("PRAGMA table_info(owned_books)")}
        for col, decl in [("authors", "TEXT"), ("narrators", "TEXT"),
                          ("series_title", "TEXT"),
                          ("series_sequence", "TEXT"),
                          ("series_asin", "TEXT"),
                          ("release_date", "TEXT"),
                          ("runtime_minutes", "INTEGER"),
                          ("language", "TEXT"), ("image_url", "TEXT"),
                          ("url", "TEXT"),
                          ("preordered", "INTEGER NOT NULL DEFAULT 0")]:
            if col not in owned_cols:  # migrate pre-library-tab databases
                c.execute(f"ALTER TABLE owned_books ADD COLUMN {col} {decl}")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                      (k, v))


# ---------- settings ----------

def get_settings() -> dict:
    with conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in rows})
    return s


def save_settings(values: dict):
    with conn() as c:
        for k, v in values.items():
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, str(v)),
            )


def secret_key() -> str:
    """The Flask session key: random, created once per install."""
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO app_secrets (key, value) "
                  "VALUES ('flask_secret_key', ?)", (secrets.token_hex(32),))
        return c.execute("SELECT value FROM app_secrets "
                         "WHERE key = 'flask_secret_key'").fetchone()[0]


# ---------- watches ----------

def add_watch(wtype: str, name: str, asin: str | None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO watches (type, name, asin, created_at) "
            "VALUES (?, ?, ?, ?)",
            (wtype, name.strip(), asin, now_iso()),
        )
        if cur.rowcount == 0:
            row = c.execute("SELECT id FROM watches WHERE type=? AND name=?",
                            (wtype, name.strip())).fetchone()
            return row["id"]
        return cur.lastrowid


def remove_watch(watch_id: int):
    with conn() as c:
        c.execute("DELETE FROM watches WHERE id = ?", (watch_id,))


def list_watches() -> list[dict]:
    today = date.today().isoformat()
    with conn() as c:
        rows = c.execute("""
            SELECT w.*,
                   COUNT(wb.asin)                       AS book_count,
                   SUM(wb.is_new)                       AS new_count,
                   MAX(b.release_date)                  AS latest_release,
                   SUM(CASE WHEN b.release_date > ?  -- TBA isn't orderable
                            THEN 1 ELSE 0 END)          AS preorder_count
            FROM watches w
            LEFT JOIN watch_books wb ON wb.watch_id = w.id AND wb.hidden = 0
            LEFT JOIN books b ON b.asin = wb.asin
            GROUP BY w.id
            ORDER BY w.type, w.name COLLATE NOCASE
        """, (today,)).fetchall()
    return [dict(r) for r in rows]


def get_watch(watch_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
    return dict(r) if r else None


def set_fetch_rev(watch_id: int, rev: int):
    with conn() as c:
        c.execute("UPDATE watches SET fetch_rev = ? WHERE id = ?",
                  (rev, watch_id))


def mark_baselined(watch_id: int):
    with conn() as c:
        c.execute("UPDATE watches SET baselined_at = ? WHERE id = ?",
                  (now_iso(), watch_id))


def reset_for_marketplace_change():
    """Another marketplace lists many books under different ASINs, so every
    watch must be re-recorded as a baseline (nothing flagged new), and the
    "sold here?" verdicts start over. Editions hidden only because they
    weren't sold in the old marketplace come back (still not new)."""
    with conn() as c:
        c.execute("UPDATE watches SET baselined_at = NULL")
        c.execute("""
            UPDATE watch_books SET hidden = 0, store_hidden = 0,
                                   region_hidden = 0, is_new = 0, notified = 1
            WHERE store_hidden = 1 OR region_hidden = 1""")
        c.execute("UPDATE books SET available = NULL, "
                  "available_checked_at = NULL")


def set_watch_asin(watch_id: int, asin: str | None):
    with conn() as c:
        c.execute("UPDATE watches SET asin = ? WHERE id = ?", (asin, watch_id))


def find_watch(wtype: str, name: str) -> int | None:
    """The id of an existing watch (name compared ignoring case, including
    accented letters: "Åsa" == "åsa"), or None."""
    want = (name or "").strip().casefold()
    with conn() as c:
        rows = c.execute("SELECT id, name FROM watches WHERE type = ?",
                         (wtype,)).fetchall()
    for r in rows:
        if r["name"].strip().casefold() == want:
            return r["id"]
    return None


def hide_other_store_links(watch_id: int, prefix: str) -> int:
    """After a marketplace change, hide this watch's books that link to
    another store (they weren't in the new store's results). Marked
    store_hidden so switching back brings them back."""
    with conn() as c:
        cur = c.execute("""
            UPDATE watch_books SET hidden = 1, store_hidden = 1, is_new = 0,
                                   notified = 1
            WHERE watch_id = ? AND hidden = 0
              AND asin IN (SELECT asin FROM books
                           WHERE url IS NOT NULL AND url NOT LIKE ? || '%')""",
            (watch_id, prefix))
        return cur.rowcount


def mark_watch_checked(watch_id: int, error: str | None = None):
    with conn() as c:
        c.execute("UPDATE watches SET last_checked_at = ?, last_error = ? WHERE id = ?",
                  (now_iso(), error, watch_id))


# ---------- books ----------

_BOOK_UPSERT = """
    INSERT INTO books
    (asin, title, subtitle, authors, narrators, series_title,
     series_sequence, all_series, release_date, runtime_minutes, language,
     image_url, url)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(asin) DO UPDATE SET
        title = excluded.title,
        subtitle = excluded.subtitle,
        authors = excluded.authors,
        narrators = excluded.narrators,
        series_title = excluded.series_title,
        series_sequence = excluded.series_sequence,
        all_series = COALESCE(excluded.all_series, books.all_series),
        release_date = excluded.release_date,
        runtime_minutes = excluded.runtime_minutes,
        language = excluded.language,
        image_url = excluded.image_url,
        url = excluded.url
"""
# published_date is deliberately not refreshed here — it comes from the
# metadata enrichment, not the Audible catalog.


# hiding also clears the NEW flag and counts as notified, so a hidden book
# never shows up as new or in an email
_HIDE = "UPDATE watch_books SET hidden = 1, is_new = 0, notified = 1 WHERE "


def _watched_series(row: dict) -> dict:
    """On a series watch, show the book's place in *that* series. A book can
    belong to several series (e.g. a sub-series and its universe); the
    stored series_title/sequence is just the first one Audible lists."""
    if row.get("watch_type") != "series" or not row.get("all_series"):
        return row
    try:
        series = json.loads(row["all_series"])
    except ValueError:
        return row
    asin = row.get("watch_asin")
    name = (row.get("watch_name") or "").strip().lower()
    for s in series:
        if ((asin and s.get("asin") == asin)
                or (s.get("title") or "").strip().lower() == name):
            row["series_title"] = s.get("title") or row.get("series_title")
            row["series_sequence"] = s.get("sequence")
            break
    return row


def _book_rows(rows) -> list[dict]:
    return [_watched_series(dict(r)) for r in rows]


def known_asins(watch_id: int) -> set[str]:
    with conn() as c:
        rows = c.execute("SELECT asin FROM watch_books WHERE watch_id = ?",
                         (watch_id,)).fetchall()
    return {r["asin"] for r in rows}


def upsert_books(watch_id: int, books: list[dict], as_new: bool,
                 hidden: bool = False) -> list[dict]:
    """Record fetched books. Catalog metadata refreshes on every check (a
    TBA placeholder can gain a real release date later); the returned list
    contains only books newly linked to this watch. hidden=True links them
    already hidden and seen (used for the language filter)."""
    existing = known_asins(watch_id)
    inserted = []
    with conn() as c:
        for b in books:
            if not b.get("asin"):
                continue
            c.execute(_BOOK_UPSERT, (
                b["asin"], b.get("title"), b.get("subtitle"),
                b.get("authors"), b.get("narrators"), b.get("series_title"),
                b.get("series_sequence"),
                json.dumps(b["all_series"]) if b.get("all_series") else None,
                b.get("release_date"),
                b.get("runtime_minutes"), b.get("language"),
                b.get("image_url"), b.get("url"),
            ))
            if b["asin"] in existing:
                continue
            c.execute("""
                INSERT OR IGNORE INTO watch_books
                (watch_id, asin, discovered_at, is_new, notified, hidden)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                watch_id, b["asin"], now_iso(),
                1 if (as_new and not hidden) else 0,
                0 if (as_new and not hidden) else 1,
                1 if hidden else 0,
            ))
            inserted.append(b)
            existing.add(b["asin"])
    return inserted


def hide_books(watch_id: int, asins: list[str]):
    """Hide the given catalog entries (e.g. wrong-language editions that were
    imported before a language preference was set)."""
    with conn() as c:
        c.executemany(_HIDE + "asin = ? AND watch_id = ?",
                      [(a, watch_id) for a in asins])


def books_for_watch(watch_id: int, include_hidden: bool = False) -> list[dict]:
    q = ("SELECT b.*, wb.watch_id, wb.discovered_at, wb.is_new, "
         "wb.notified, wb.hidden, wb.store_hidden, wb.region_hidden, "
         "wb.placeholder_hidden, wb.keep_visible, wb.mismatch_hidden, "
         "w.name AS watch_name, "
         "w.type AS watch_type, w.asin AS watch_asin "
         "FROM books b JOIN watch_books wb ON wb.asin = b.asin "
         "JOIN watches w ON w.id = wb.watch_id "
         "WHERE wb.watch_id = ? ")
    if not include_hidden:
        q += "AND wb.hidden = 0 "
    q += "ORDER BY (b.release_date IS NULL), b.release_date DESC"
    with conn() as c:
        rows = c.execute(q, (watch_id,)).fetchall()
    return _book_rows(rows)


def dedupe_by_asin(rows) -> list[dict]:
    """One entry per book, keeping list order. When a book matches several
    watches (e.g. its author and its series) the series watch wins the
    attribution."""
    best: dict[str, dict] = {}
    for r in rows:
        r = _watched_series(dict(r))
        cur = best.get(r["asin"])
        if cur is None or (r["watch_type"] == "series"
                           and cur["watch_type"] != "series"):
            best[r["asin"]] = r  # dict keeps first-seen position on replace
    return list(best.values())


def new_books(watch_id: int | None = None) -> list[dict]:
    q = ("SELECT b.*, wb.watch_id, wb.is_new, wb.hidden, "
         "w.name AS watch_name, w.type AS watch_type, "
         "w.asin AS watch_asin "
         "FROM books b "
         "JOIN watch_books wb ON wb.asin = b.asin "
         "JOIN watches w ON w.id = wb.watch_id "
         "WHERE wb.is_new = 1 AND wb.hidden = 0")
    args: tuple = ()
    if watch_id is not None:
        q += " AND wb.watch_id = ?"
        args = (watch_id,)
    q += " ORDER BY (b.release_date IS NULL), b.release_date DESC"
    with conn() as c:
        rows = c.execute(q, args).fetchall()
    if watch_id is None:  # dashboard: show each book once
        return dedupe_by_asin(rows)
    return _book_rows(rows)


def books_missing_published(limit: int = 30) -> list[dict]:
    """Visible books whose original publish date hasn't been looked up yet,
    pre-orders and newest releases first. NULL = never looked up; '' = looked
    up, nothing found (won't be retried)."""
    today = date.today().isoformat()
    with conn() as c:
        rows = c.execute("""
            SELECT b.asin, b.title, b.authors
            FROM books b
            WHERE b.published_date IS NULL
              AND EXISTS (SELECT 1 FROM watch_books wb
                          WHERE wb.asin = b.asin AND wb.hidden = 0)
            ORDER BY (b.release_date IS NULL OR b.release_date > ?) DESC,
                     b.release_date DESC
            LIMIT ?
        """, (today, limit)).fetchall()
    return [dict(r) for r in rows]


def set_published_date(asin: str, value: str):
    with conn() as c:
        c.execute("UPDATE books SET published_date = ? WHERE asin = ?",
                  (value, asin))


def books_missing_availability(limit: int = 50) -> list[dict]:
    """Visible books not yet validated against the marketplace's own store
    listing, pre-orders and newest first. TBA books (no release date) are
    excluded — unannounced titles often aren't in the store search yet, so
    "absent" proves nothing; they're checked once they gain a date."""
    today = date.today().isoformat()
    with conn() as c:
        rows = c.execute("""
            SELECT b.asin, b.title, b.authors
            FROM books b
            WHERE b.available IS NULL AND b.title IS NOT NULL
              AND b.release_date IS NOT NULL
              AND EXISTS (SELECT 1 FROM watch_books wb
                          WHERE wb.asin = b.asin AND wb.hidden = 0)
            ORDER BY (b.release_date > ?) DESC, b.release_date DESC
            LIMIT ?
        """, (today, limit)).fetchall()
    return [dict(r) for r in rows]


def set_availability(asin: str, available: bool):
    with conn() as c:
        c.execute("UPDATE books SET available = ?, available_checked_at = ? "
                  "WHERE asin = ?", (1 if available else 0, now_iso(), asin))


REGION_RECHECK_DAYS = 7


def books_to_recheck(limit: int = 20) -> list[dict]:
    """Books hidden as "not sold in this store" that are due another look
    (weekly, oldest check first). Stores add editions over time, and the
    store search can simply miss a book, so "no" is never final."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=REGION_RECHECK_DAYS)).strftime(
                  "%Y-%m-%dT%H:%M:%SZ")
    with conn() as c:
        rows = c.execute("""
            SELECT b.asin, b.title, b.authors FROM books b
            WHERE b.available = 0
              AND (b.available_checked_at IS NULL
                   OR b.available_checked_at < ?)
              AND EXISTS (SELECT 1 FROM watch_books wb
                          WHERE wb.asin = b.asin AND wb.region_hidden = 1)
            ORDER BY b.available_checked_at IS NOT NULL,
                     b.available_checked_at
            LIMIT ?""", (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


def mark_availability_checked(asin: str):
    with conn() as c:
        c.execute("UPDATE books SET available_checked_at = ? WHERE asin = ?",
                  (now_iso(), asin))


def restore_now_available(asin: str) -> int:
    """A book hidden as "not sold here" is sold here now: bring it back on
    the watches it was auto-hidden from, as a new book (it's new to you),
    so it shows on the dashboard and goes out in the next email. Links you
    hid yourself stay hidden."""
    with conn() as c:
        c.execute("UPDATE books SET available = 1, available_checked_at = ? "
                  "WHERE asin = ?", (now_iso(), asin))
        cur = c.execute("""
            UPDATE watch_books SET hidden = 0, region_hidden = 0, is_new = 1,
                                   notified = 0
            WHERE asin = ? AND region_hidden = 1""", (asin,))
        return cur.rowcount


def hide_asin_everywhere(asin: str):
    """Hide a book on every watch (e.g. an edition not sold in this
    marketplace). Only visible links are touched and they're marked
    region_hidden, so the weekly re-check can bring them back; a link you
    unhid yourself is never re-hidden (the book is only judged once until
    then)."""
    with conn() as c:
        c.execute("UPDATE watch_books SET hidden = 1, region_hidden = 1, "
                  "is_new = 0, notified = 1 WHERE asin = ? AND hidden = 0 "
                  "AND keep_visible = 0", (asin,))


def series_sizes() -> dict[str, int]:
    """Known number of distinct catalog entries per series title (from every
    book any watch has surfaced)."""
    with conn() as c:
        rows = c.execute(
            "SELECT series_title, COUNT(1) AS n FROM books "
            "WHERE series_title IS NOT NULL GROUP BY series_title").fetchall()
    return {r["series_title"]: r["n"] for r in rows}


def preorder_books() -> list[dict]:
    """Visible books you can pre-order across all watches, soonest release
    first, deduped when a book matches multiple watches (the series watch wins
    the attribution). TBA entries (no release date) are excluded: Audible
    only opens a store page once a title has a date, so they're dead links.
    A check refreshes release dates, so a TBA book appears here once dated."""
    today = date.today().isoformat()
    with conn() as c:
        rows = c.execute("""
            SELECT b.*, wb.watch_id, wb.is_new,
                   w.name AS watch_name, w.type AS watch_type,
                   w.asin AS watch_asin
            FROM books b
            JOIN watch_books wb ON wb.asin = b.asin
            JOIN watches w ON w.id = wb.watch_id
            WHERE wb.hidden = 0 AND b.release_date > ?
            ORDER BY b.release_date
        """, (today,)).fetchall()
    return dedupe_by_asin(rows)


def unnotified_books() -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT b.*, wb.watch_id, wb.is_new,
                   w.name AS watch_name, w.type AS watch_type,
                   w.asin AS watch_asin
            FROM books b
            JOIN watch_books wb ON wb.asin = b.asin
            JOIN watches w ON w.id = wb.watch_id
            WHERE wb.notified = 0 AND wb.hidden = 0
            ORDER BY (b.release_date IS NULL), b.release_date DESC
        """).fetchall()
    return _book_rows(rows)


def mark_notified(pairs: list[tuple[str, int]]):
    with conn() as c:
        c.executemany("UPDATE watch_books SET notified = 1 "
                      "WHERE asin = ? AND watch_id = ?", pairs)


def mark_seen(asin: str):
    """Seen is about the book, so clear it on every watch it matches —
    otherwise its other watch's copy would pop up on the dashboard."""
    with conn() as c:
        c.execute("UPDATE watch_books SET is_new = 0 WHERE asin = ?", (asin,))


def set_book_hidden(asin: str, watch_id: int, hidden: bool):
    """Hide/unhide a catalog entry. The link row is kept (not deleted) so the
    next Audible check doesn't re-import it and flag it as a new release."""
    with conn() as c:
        # your choice replaces any automatic reason, so the re-checks
        # never undo it (a book you hid stays hidden; one you unhid stays)
        if hidden:
            c.execute(_HIDE + "asin = ? AND watch_id = ?", (asin, watch_id))
            c.execute("UPDATE watch_books SET region_hidden = 0, "
                      "store_hidden = 0, placeholder_hidden = 0, "
                      "mismatch_hidden = 0, keep_visible = 0 "
                      "WHERE asin = ? AND watch_id = ?",
                      (asin, watch_id))
        else:
            c.execute("UPDATE watch_books SET hidden = 0, region_hidden = 0, "
                      "store_hidden = 0, placeholder_hidden = 0, "
                      "mismatch_hidden = 0, keep_visible = 1 "
                      "WHERE asin = ? AND watch_id = ?",
                      (asin, watch_id))


# Versions that are separate products even with the same series number:
# a no-date stub of one is never matched against a dated listing of another.
_ALT_VERSION = re.compile(
    r"dramati[sz]ed|full[- ]cast|adaptation|radio (drama|play)|graphic ?audio",
    re.IGNORECASE)


def _is_alt_version(title: str | None) -> bool:
    return bool(_ALT_VERSION.search(title or ""))


def hide_not_by_author(watch_id: int, author: str,
                       current: set[str]) -> int:
    """Hide books on an author watch that aren't that author's audiobooks:
    entries from before exact author matching (e.g. "Scott Meyers" on a
    "Scott Meyer" watch) and podcasts. A book is kept if this check's fetch
    returned it (`current`) or its credits name the author. Hidden the
    normal way (Unhide works and sticks), never emailed."""
    import audible_api  # (db stays importable on its own)
    target = audible_api.norm_name(author)
    stale = []
    for b in books_for_watch(watch_id):
        if b["keep_visible"] or b["asin"] in current:
            continue
        credited = audible_api._credited_names(
            audible_api.split_authors(b["authors"]))
        if target not in credited:
            stale.append(b["asin"])
    if stale:
        with conn() as c:
            c.executemany("UPDATE watch_books SET hidden = 1, is_new = 0, "
                          "notified = 1, mismatch_hidden = 1 "
                          "WHERE asin = ? AND watch_id = ? AND hidden = 0 "
                          "AND keep_visible = 0",
                          [(a, watch_id) for a in stale])
    return len(stale)


def hide_podcasts(watch_id: int, asins: set[str]) -> int:
    """Hide podcasts linked to a watch (they aren't audiobooks)."""
    if not asins:
        return 0
    with conn() as c:
        cur = c.executemany("UPDATE watch_books SET hidden = 1, is_new = 0, "
                            "notified = 1, mismatch_hidden = 1 "
                            "WHERE asin = ? AND watch_id = ? AND hidden = 0 "
                            "AND keep_visible = 0",
                            [(a, watch_id) for a in asins])
        return cur.rowcount


def hide_placeholders(watch_id: int) -> int:
    """Hide no-date placeholders on a watch: an entry without a release date
    when the same series and book number already has a dated listing of the
    same kind (e.g. a "Defiant" stub next to the real Skyward #4). Hidden
    the normal way, so it shows under Hidden entries with Unhide; a book
    you unhid yourself is never hidden again, and it's marked notified so
    a placeholder is never emailed.

    Alternate versions are never touched: they're dated listings of their
    own (dramatized adaptations, other narrators, re-releases)."""
    groups: dict = {}
    for b in books_for_watch(watch_id):
        seq = (b["series_sequence"] or "").strip()
        if not seq:
            continue
        key = ((b["series_title"] or "").strip().casefold(), seq)
        groups.setdefault(key, []).append(b)
    stubs = []
    for group in groups.values():
        dated_kinds = {_is_alt_version(b["title"]) for b in group
                       if b["release_date"]}
        for b in group:
            if (not b["release_date"] and not b["keep_visible"]
                    and _is_alt_version(b["title"]) in dated_kinds):
                stubs.append(b["asin"])
    if stubs:
        with conn() as c:
            c.executemany("UPDATE watch_books SET hidden = 1, is_new = 0, "
                          "notified = 1, placeholder_hidden = 1 "
                          "WHERE asin = ? AND watch_id = ? AND hidden = 0 "
                          "AND keep_visible = 0",
                          [(a, watch_id) for a in stubs])
    return len(stubs)

def mark_all_seen(watch_id: int | None = None):
    with conn() as c:
        if watch_id is None:
            c.execute("UPDATE watch_books SET is_new = 0")
        else:
            c.execute("UPDATE watch_books SET is_new = 0 WHERE watch_id = ?",
                      (watch_id,))


# ---------- owned library (from connected Audible account) ----------

def replace_owned(rows: list[dict]):
    """Replace the owned-library snapshot with freshly synced book dicts."""
    with conn() as c:
        c.execute("DELETE FROM owned_books")
        c.executemany("""
            INSERT OR IGNORE INTO owned_books
            (asin, title, authors, narrators, series_title, series_sequence,
             series_asin, release_date, runtime_minutes, language, image_url,
             url, purchase_date, preordered)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [(b["asin"], b.get("title"), b.get("authors"),
               b.get("narrators"), b.get("series_title"),
               b.get("series_sequence"), b.get("series_asin"),
               b.get("release_date"), b.get("runtime_minutes"),
               b.get("language"), b.get("image_url"), b.get("url"),
               b.get("purchase_date"), 1 if b.get("preordered") else 0)
              for b in rows if b.get("asin")])


def owned_books_list() -> list[dict]:
    """The synced library: pre-orders that haven't come out yet first
    (soonest release first, undated last), then most recent purchases
    first. A pre-order that's already out is listed like any purchase."""
    today = date.today().isoformat()
    with conn() as c:
        rows = c.execute("""
            SELECT *, (preordered = 1 AND (release_date IS NULL
                                           OR release_date > ?)) AS upcoming
            FROM owned_books WHERE title IS NOT NULL
            ORDER BY upcoming DESC,
                     CASE WHEN upcoming THEN (release_date IS NULL) END,
                     CASE WHEN upcoming THEN release_date END,
                     (purchase_date IS NULL), purchase_date DESC,
                     title COLLATE NOCASE""", (today,)).fetchall()
    return [dict(r) for r in rows]


def owned_preorders() -> list[dict]:
    """The pre-order rows from the last library sync."""
    with conn() as c:
        rows = c.execute("SELECT * FROM owned_books WHERE preordered = 1"
                         ).fetchall()
    return [dict(r) for r in rows]


def owned_asins() -> set[str]:
    with conn() as c:
        return {r["asin"] for r in
                c.execute("SELECT asin FROM owned_books").fetchall()}


def owned_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(1) FROM owned_books").fetchone()[0]


# ---------- wishlist (price alerts) ----------

def replace_wishlist(rows: list[dict]):
    """Store a freshly fetched wishlist. Titles that left the wishlist are
    dropped; titles still on it keep their own target price, alert state
    and lowest price seen."""
    now = now_iso()
    asins = [b["asin"] for b in rows if b.get("asin")]
    with conn() as c:
        if asins:
            marks = ",".join("?" * len(asins))
            c.execute(f"DELETE FROM wishlist WHERE asin NOT IN ({marks})",
                      asins)
        else:
            c.execute("DELETE FROM wishlist")
        for pos, b in enumerate(r for r in rows if r.get("asin")):
            c.execute("""
                INSERT INTO wishlist
                (asin, title, authors, narrators, series_title,
                 series_sequence, release_date, image_url, url, price,
                 list_price, price_type, currency, lowest_seen, position,
                 added_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    title = excluded.title, authors = excluded.authors,
                    narrators = excluded.narrators,
                    series_title = excluded.series_title,
                    series_sequence = excluded.series_sequence,
                    release_date = excluded.release_date,
                    image_url = excluded.image_url, url = excluded.url,
                    price = excluded.price,
                    list_price = excluded.list_price,
                    price_type = excluded.price_type,
                    currency = excluded.currency,
                    lowest_seen = CASE
                        WHEN excluded.price IS NULL THEN wishlist.lowest_seen
                        WHEN wishlist.lowest_seen IS NULL
                          OR excluded.price < wishlist.lowest_seen
                        THEN excluded.price
                        ELSE wishlist.lowest_seen END,
                    position = excluded.position,
                    updated_at = excluded.updated_at
            """, (b["asin"], b.get("title"), b.get("authors"),
                  b.get("narrators"), b.get("series_title"),
                  b.get("series_sequence"), b.get("release_date"),
                  b.get("image_url"), b.get("url"), b.get("price"),
                  b.get("list_price"), b.get("price_type"),
                  b.get("currency"), b.get("price"), pos, now, now))


def wishlist_items() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM wishlist ORDER BY position, title"
                         ).fetchall()
    return [dict(r) for r in rows]


def wishlist_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(1) FROM wishlist").fetchone()[0]


def set_wishlist_target(asin: str, target: float | None) -> bool:
    """Set (or with None, clear) one title's alert price. A new target
    re-arms the alert, so a title already at or below it is reported on
    the next check. Returns whether the title is on the wishlist."""
    with conn() as c:
        cur = c.execute("UPDATE wishlist SET target_price = ?, "
                        "alerted_price = NULL WHERE asin = ?",
                        (target, asin))
        return cur.rowcount > 0


def mark_price_alerted(prices: dict[str, float]):
    with conn() as c:
        c.executemany("UPDATE wishlist SET alerted_price = ? WHERE asin = ?",
                      [(p, a) for a, p in prices.items()])


def rearm_price_alerts(asins: list[str]):
    with conn() as c:
        c.executemany("UPDATE wishlist SET alerted_price = NULL "
                      "WHERE asin = ?", [(a,) for a in asins])


def rearm_all_price_alerts():
    """After the default alert price changes, every title is judged
    afresh against it."""
    with conn() as c:
        c.execute("UPDATE wishlist SET alerted_price = NULL")


# ---------- check log ----------

def log_check(trigger: str, new_count: int, errors: str | None, email_sent: bool):
    with conn() as c:
        c.execute(
            "INSERT INTO check_log (ran_at, trigger, new_books, errors, email_sent) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso(), trigger, new_count, errors, 1 if email_sent else 0))


def recent_checks(limit: int = 10) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM check_log ORDER BY id DESC LIMIT ?",
                         (limit,)).fetchall()
    return [dict(r) for r in rows]
