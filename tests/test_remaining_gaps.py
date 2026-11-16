"""Tests for the last review items: input validation, sending email over
SMTP (faked), print-date lookups (faked HTTP), database migration from the
first version and local-time display."""
import email
import smtplib
import sqlite3
import ssl

import pytest

import app as webapp
import audible_api
import bookinfo
import checker
import db
import mock_api


# ---- bad input gets a clean answer, never HTTP 500 --------------------------

def test_non_ascii_csrf_token_is_rejected_cleanly(client):
    resp = client.post("/seen-all", data={"csrf_token": "tök"})
    assert resp.status_code == 302  # refused cleanly, not a crash


def test_unknown_watch_type_is_rejected(client):
    resp = client.post("/watch/add", data={"type": "bogus", "name": "X"},
                       headers={"X-Requested-With": "fetch"})
    assert resp.status_code == 400 and db.list_watches() == []


@pytest.mark.parametrize("port, saved", [("abc", False), ("0", False),
                                         ("70000", False), ("587", True),
                                         ("", True)])
def test_smtp_port_is_validated(client, port, saved):
    client.post("/settings", data={"check_hour": "8", "smtp_port": port})
    assert (db.get_settings()["smtp_port"] == port) is saved


def test_watch_deleted_during_a_check_is_not_an_error(watched, monkeypatch):
    real = audible_api.fetch_watch_books

    def fetch_then_delete(wtype, name, asin, marketplace):
        books = real(wtype, name, asin, marketplace)
        if wtype == "series":
            db.remove_watch(watched["series"])
        return books
    monkeypatch.setattr(audible_api, "fetch_watch_books", fetch_then_delete)
    assert checker.check_all()["errors"] == []


# ---- email -------------------------------------------------------------------

class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def sendmail(self, sender, recipients, message):
        self.calls.append(("sendmail", sender, recipients))
        self.message = email.message_from_string(message)


SETTINGS = {"smtp_host": "smtp.example.com", "smtp_port": "465",
            "smtp_user": "me@example.com", "smtp_password": "pw",
            "email_from": "", "email_to": "a@example.com, b@example.com",
            "smtp_security": "ssl"}


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP.instances


def test_send_email_over_ssl(smtp):
    checker.send_email("Subject", "<p>hi</p>", "hi", SETTINGS)
    s = smtp[0]
    assert (s.host, s.port) == ("smtp.example.com", 465)
    assert s.context is not None and "starttls" not in s.calls
    assert ("login", "me@example.com", "pw") in s.calls
    assert ("sendmail", "me@example.com",
            ["a@example.com", "b@example.com"]) in s.calls
    assert s.message["Date"] and s.message["Message-ID"].endswith(
        "@example.com>")


def test_send_email_over_starttls(smtp):
    checker.send_email("S", "<p>x</p>", "x",
                       dict(SETTINGS, smtp_security="starttls",
                            smtp_port="587"))
    assert smtp[0].calls[0] == "starttls"
    assert smtp[0].port == 587


def test_missing_credentials_are_explained(smtp):
    with pytest.raises(RuntimeError, match="not configured"):
        checker.send_email("S", "h", "t", dict(SETTINGS, smtp_password=""))
    assert smtp == []


def test_email_html_is_escaped():
    book = {"title": "Q&A <Live>", "authors": "A & B", "url": "https://x/?a=1&b=2",
            "image_url": 'https://x/"c.jpg', "series_title": "<Saga>",
            "series_sequence": "1", "release_date": "2099-01-01",
            "watch_name": "Tom & Jerry", "watch_type": "series"}
    _, body, text = checker.build_email([book])
    assert "Q&amp;A &lt;Live&gt;" in body and "&lt;Saga&gt;" in body
    assert "<Live>" not in body and 'x/"c.jpg' not in body
    assert "Q&A <Live>" in text  # the plain-text part stays as-is


def test_certificate_error_hint():
    hint = checker.explain_smtp_error(
        ssl.SSLCertVerificationError("certificate verify failed"))
    assert "certificate" in hint and "port and security" not in hint


# ---- print publish dates (Google Books, Open Library) -----------------------

class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload or {}

    def json(self):
        return self._payload


@pytest.fixture
def lookups(monkeypatch):
    """Route bookinfo's requests by URL to scripted responses."""
    routes = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        r = routes[url]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(bookinfo.requests, "get", fake_get)
    monkeypatch.setattr(bookinfo, "published_date",
                        mock_api.REAL["published_date"])
    return routes


def _google(*vols):
    return Resp(payload={"items": [{"volumeInfo": v} for v in vols]})


def test_google_earliest_matching_edition(lookups):
    lookups[bookinfo.GOOGLE_URL] = _google(
        {"title": "The Way of Kings", "publishedDate": "2011-05-24"},
        {"title": "The Way of Kings: Book One", "publishedDate": "2010-08-31"},
        {"title": "Kings of the Wyld", "publishedDate": "2001-01-01"})
    assert bookinfo.published_date("The Way of Kings") == ("2010-08-31", True)


def test_open_library_fallback(lookups):
    lookups[bookinfo.GOOGLE_URL] = _google()
    lookups[bookinfo.OPENLIB_URL] = Resp(payload={"docs": [
        {"title": "Dune", "first_publish_year": 1965}]})
    assert bookinfo.published_date("Dune", "Frank Herbert") == ("1965", True)


def test_one_source_down_is_not_a_definitive_miss(lookups):
    lookups[bookinfo.GOOGLE_URL] = Resp(status=429)
    lookups[bookinfo.OPENLIB_URL] = Resp(payload={"docs": []})
    assert bookinfo.published_date("Obscure") == (None, False)


def test_both_sources_down_raises(lookups):
    lookups[bookinfo.GOOGLE_URL] = Resp(status=429)
    lookups[bookinfo.OPENLIB_URL] = bookinfo.requests.ConnectionError("down")
    with pytest.raises(bookinfo.RateLimited):
        bookinfo.published_date("Anything")


def test_no_match_anywhere_is_definitive(lookups):
    lookups[bookinfo.GOOGLE_URL] = _google()
    lookups[bookinfo.OPENLIB_URL] = Resp(payload={"docs": []})
    assert bookinfo.published_date("Nothing") == (None, True)


# ---- migrating a database from the first version ---------------------------

def test_v1_database_migrates(tmp_path, monkeypatch):
    path = str(tmp_path / "v1.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE watches (id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL, name TEXT NOT NULL, asin TEXT,
            created_at TEXT NOT NULL, last_checked_at TEXT, last_error TEXT,
            UNIQUE (type, name));
        CREATE TABLE books (asin TEXT, watch_id INTEGER, title TEXT,
            subtitle TEXT, authors TEXT, narrators TEXT, series_title TEXT,
            series_sequence TEXT, release_date TEXT, runtime_minutes INTEGER,
            image_url TEXT, url TEXT, discovered_at TEXT, is_new INTEGER,
            notified INTEGER, PRIMARY KEY (asin, watch_id));
        CREATE TABLE owned_books (asin TEXT PRIMARY KEY, title TEXT,
            purchase_date TEXT);
        INSERT INTO watches VALUES (1, 'author', 'Old Author', NULL,
            '2025-01-01T00:00:00Z', '2025-02-01T00:00:00Z', NULL);
        INSERT INTO books VALUES ('OLD1', 1, 'Old Book', '', 'Old Author',
            '', NULL, NULL, '2024-01-01', 600, NULL, 'u',
            '2025-01-01T00:00:00Z', 1, 0);
    """)
    c.commit()
    c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    [w] = db.list_watches()
    assert w["baselined_at"] == "2025-02-01T00:00:00Z"
    [b] = db.books_for_watch(1)
    assert (b["asin"], b["title"], b["is_new"], b["hidden"]) == \
        ("OLD1", "Old Book", 1, 0)
    db.init_db()  # running it again changes nothing
    assert len(db.books_for_watch(1)) == 1


# ---- local time display -------------------------------------------------------

def test_localtime_filter(monkeypatch):
    out = webapp.localtime_filter("2026-11-15T21:41:14Z")
    assert len(out) == 16 and out[4] == "-" and out[13] == ":"
    assert webapp.localtime_filter(None) == "never"
    assert webapp.localtime_filter("garbage") == "garbage"


def test_pages_show_local_times(client, watched):
    checker.check_all()
    html = client.get("/").get_data(as_text=True)
    assert "When (UTC)" not in html
    assert not any("T" in cell and cell.endswith("Z")
                   for cell in html.split(">") if cell[:2] == "20")


# ---- health check -----------------------------------------------------------

def test_healthz_identifies_the_app(client, monkeypatch):
    monkeypatch.setenv("AW_PASSWORD", "x")  # works without signing in
    assert client.get("/healthz").get_json()["app"] == "audible-watcher"
