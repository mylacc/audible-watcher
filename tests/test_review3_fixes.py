"""Regression tests for the third review (2026-11-15). Live-catalog checks
for the search fixes are in live_tests/ (they need the network)."""
import os
from datetime import datetime, timedelta, timezone

import pytest

import account
import app as webapp
import audible_api
import checker
import db
import mock_api
import paths
from mock_api import make_product


@pytest.fixture
def spy_params(monkeypatch):
    """Record the parameters of every catalog query."""
    calls = []
    real = audible_api._get

    def spy(marketplace, params):
        calls.append(dict(params))
        return real(marketplace, params)
    monkeypatch.setattr(audible_api, "_get", spy)
    return calls


@pytest.fixture(autouse=True)
def no_lockouts():
    webapp._login_failures.clear()
    yield
    webapp._login_failures.clear()


# ---- 1, 2. searches use relevance order --------------------------------------

def test_series_search_uses_relevance_order(spy_params):
    audible_api.search_series_candidates("Great Series", "us")
    assert spy_params and "products_sort_by" not in spy_params[-1]


def test_author_search_page_uses_relevance_and_more_results(client,
                                                            spy_params):
    client.get("/search?q=Test+Author")
    author_calls = [p for p in spy_params if "author" in p]
    assert author_calls and "products_sort_by" not in author_calls[0]
    assert author_calls[0]["num_results"] == 50


def test_author_watch_still_newest_first_and_deeper(spy_params):
    audible_api.fetch_watch_books("author", "Test Author", None, "us")
    assert spy_params[0]["products_sort_by"] == "-ReleaseDate"


# ---- 3. marketplace change: per-store series IDs, old editions hidden -------

def test_marketplace_change_resolves_series_in_new_store(client, watched,
                                                         monkeypatch):
    sid = watched["series"]
    client.post("/settings", data={"check_hour": "8", "marketplace": "uk"})
    uk = [dict(p, asin="UK" + p["asin"],
               series=[dict(s, asin="UKSER") for s in p.get("series", [])])
          for p in mock_api.CATALOG]
    monkeypatch.setattr(mock_api, "CATALOG", uk)
    assert checker.check_all()["new_books"] == 0
    w = db.get_watch(sid)
    assert w["asin"] == "UKSER"          # the UK store's own series ID
    visible = db.books_for_watch(sid)
    assert {b["asin"] for b in visible} == {"UKB001", "UKB002"}
    assert all(".co.uk/" in b["url"] for b in visible)  # US editions hidden
    assert db.new_books() == []

    # switching back brings the US editions back (still not new)
    client.post("/settings", data={"check_hour": "8", "marketplace": "us"})
    monkeypatch.setattr(mock_api, "CATALOG", mock_api.CATALOG)
    hidden_us = [b for b in db.books_for_watch(sid, include_hidden=True)
                 if b["asin"] in ("B001", "B002")]
    assert hidden_us and not any(b["hidden"] for b in hidden_us)


def test_series_finding_nothing_is_flagged(client):
    client.post("/watch/add", data={"type": "series", "name": "No Such",
                                    "asin": "NOPE"})
    w = next(w for w in db.list_watches() if w["type"] == "series")
    assert "no books for this series" in (w["last_error"] or "")


# ---- 4. sign-in redirect -------------------------------------------------------

@pytest.mark.parametrize("target", ["/\t/evil.example", "/\n/evil.example",
                                    "/\\evil.example", "//evil.example",
                                    "https://evil.example", "/./\\evil"])
def test_sign_in_redirect_stays_on_site(target):
    with webapp.app.test_request_context():
        assert webapp._safe_next(target) == "/"


def test_sign_in_redirect_keeps_real_paths():
    with webapp.app.test_request_context():
        assert webapp._safe_next("/watch/3?view=series") == \
            "/watch/3?view=series"


# ---- 5. re-adding a watch ------------------------------------------------------

def test_readding_a_watch_keeps_new_releases(client, watched):
    mock_api.state["include_new"] = True       # B004 appears on Audible
    resp = client.post("/watch/add", data={"type": "author",
                                           "name": "test author"})
    assert resp.status_code == 302 and "/watch/" in resp.location
    checker.check_all()
    assert {b["asin"] for b in db.new_books()} == {"B004"}


def test_search_marks_watched_results(client, watched):
    html = client.get("/search?q=Test+Author").get_data(as_text=True)
    assert "Watching ✓" in html
    # no second "Watch author" form for it
    assert '<input type="hidden" name="name" value="Test Author">' not in html


# ---- 6. sign-in lockout --------------------------------------------------------

def _login(client, password, ip="10.9.9.1"):
    return client.post("/login", data={"password": password},
                       environ_base={"REMOTE_ADDR": ip})


def test_wrong_passwords_lock_out_that_address(client, monkeypatch):
    monkeypatch.setenv("AW_PASSWORD", "s3cret")
    for _ in range(webapp.FREE_ATTEMPTS):
        assert _login(client, "nope").status_code == 200
    _login(client, "nope")                      # one too many
    locked = _login(client, "s3cret")           # even the right one waits
    assert locked.status_code == 429
    assert "Wait" in locked.get_data(as_text=True)
    other = _login(client, "s3cret", ip="10.9.9.2")
    assert other.status_code == 302             # other addresses unaffected


def test_lockout_expires(client, monkeypatch):
    monkeypatch.setenv("AW_PASSWORD", "s3cret")
    for _ in range(webapp.FREE_ATTEMPTS + 1):
        _login(client, "nope")
    clock = [webapp.time.monotonic()]
    monkeypatch.setattr(webapp.time, "monotonic", lambda: clock[0])
    assert _login(client, "s3cret").status_code == 429
    clock[0] += webapp.FIRST_LOCKOUT + 1
    assert _login(client, "s3cret").status_code == 302


def test_other_addresses_cannot_lock_you_out(client, monkeypatch):
    """No shared limit: wrong guesses from many addresses must not lock out
    someone else (review 4: anyone on the network could lock everyone out)."""
    monkeypatch.setenv("AW_PASSWORD", "s3cret")
    for i in range(30):
        _login(client, "nope", ip=f"10.8.0.{i}")
    assert _login(client, "s3cret", ip="10.8.1.1").status_code == 302


def test_wrong_password_does_not_sleep(client, monkeypatch):
    monkeypatch.setenv("AW_PASSWORD", "s3cret")
    monkeypatch.setattr(webapp.time, "sleep",
                        lambda s: pytest.fail("must not block a thread"))
    _login(client, "nope")


# ---- 7, 8. outages ---------------------------------------------------------------

def test_outage_with_few_watches_still_retries(monkeypatch):
    db.add_watch("author", "Only Author", None)

    def down(*a, **k):
        raise audible_api.AudibleUnavailable("timed out")
    monkeypatch.setattr(audible_api, "fetch_watch_books", down)
    result = checker.check_all()
    assert result["aborted"]
    assert "unreachable" in result["errors"][-1]


def test_series_added_during_outage_gets_its_author_later(client,
                                                          monkeypatch):
    real = audible_api.fetch_watch_books

    def down(*a, **k):
        raise audible_api.AudibleUnavailable("timed out")
    monkeypatch.setattr(audible_api, "fetch_watch_books", down)
    client.post("/watch/add", data={"type": "series", "name": "Great Series",
                                    "asin": "SER123"})
    monkeypatch.setattr(audible_api, "fetch_watch_books", real)
    checker.check_all()
    kinds = {(w["type"], w["name"]) for w in db.list_watches()}
    assert ("author", "Test Author") in kinds
    assert db.new_books() == []


def test_removed_author_is_not_readded_after_marketplace_change(client,
                                                                watched):
    db.remove_watch(watched["author"])
    client.post("/settings", data={"check_hour": "8", "marketplace": "uk"})
    checker.check_all()
    assert all(w["type"] == "series" for w in db.list_watches())


# ---- 9. names with commas --------------------------------------------------------

def test_split_authors_keeps_suffixes():
    assert audible_api.split_authors(
        "Martin Luther King, Jr., Coretta Scott King, Ann Writer") == \
        ["Martin Luther King, Jr.", "Coretta Scott King", "Ann Writer"]
    assert audible_api.split_authors("") == []
    assert audible_api.first_author("A, B") == "A"


def test_comma_names_stay_whole(client, monkeypatch):
    monkeypatch.setattr(mock_api, "CATALOG", mock_api.CATALOG + [
        make_product("B020", "Dream Speech", ["Martin Luther King, Jr."],
                     "2020-01-01")])
    html = client.get("/search?q=Martin").get_data(as_text=True)
    assert 'value="Martin Luther King, Jr."' in html
    client.post("/watch/add", data={"type": "author",
                                    "name": "Martin Luther King, Jr."})
    w = db.list_watches()[0]
    assert w["book_count"] == 1 and not w["last_error"]
    page = client.get(f"/watch/{w['id']}").get_data(as_text=True)
    assert ">Martin Luther King, Jr.</a>" in page


# ---- 10, 14. refused page stylesheet, referrer -----------------------------------

def test_refused_page_can_load_its_stylesheet(client):
    resp = client.get("/static/style.css", headers={"Host": "nas.example.com"})
    assert resp.status_code == 200
    assert client.get("/", headers={"Host": "nas.example.com"}).status_code \
        == 400


def test_pages_send_no_referrer(client):
    for page in ("/", "/settings"):
        assert 'name="referrer" content="no-referrer"' in \
            client.get(page).get_data(as_text=True)
    refused = client.get("/", headers={"Host": "x.example.com"})
    assert 'content="no-referrer"' in refused.get_data(as_text=True)


# ---- 11. Message-ID ----------------------------------------------------------------

def test_message_id_with_display_name_from(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def sendmail(self, sender, to, message):
            captured["msg"] = message
    monkeypatch.setattr(checker.smtplib, "SMTP_SSL", FakeSMTP)
    checker.send_email("S", "<p>h</p>", "t", {
        "smtp_host": "h", "smtp_port": "465", "smtp_security": "ssl",
        "smtp_user": "me@example.com",
        "smtp_password": "pw", "email_to": "",
        "email_from": "Audible Watcher <me@example.com>"})
    line = next(l for l in captured["msg"].splitlines()
                if l.startswith("Message-ID:"))
    assert line.endswith("@example.com>") and ">>" not in line


# ---- 12. settings validation --------------------------------------------------------

@pytest.mark.parametrize("field, value", [("marketplace", "narnia"),
                                          ("language", "klingon"),
                                          ("smtp_security", "rot13")])
def test_unknown_choices_are_rejected(client, field, value):
    before = db.get_settings()[field]
    resp = client.post("/settings", data={"check_hour": "8", field: value},
                       follow_redirects=True)
    assert "not saved" in resp.get_data(as_text=True)
    assert db.get_settings()[field] == before


# ---- 13. the owned library keeps itself fresh -----------------------------------------

def test_library_resyncs_when_stale(monkeypatch):
    synced = []
    monkeypatch.setattr(account, "is_connected", lambda: True)
    monkeypatch.setattr(account, "sync_library",
                        lambda: synced.append(1) or 7)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    db.save_settings({"library_synced_at": long_ago})
    webapp._refresh_library_if_stale()
    assert synced == [1]
    db.save_settings({"library_synced_at": db.now_iso()})
    webapp._refresh_library_if_stale()
    assert synced == [1]                  # fresh: no second sync


def test_library_sync_failure_never_breaks_a_check(monkeypatch):
    monkeypatch.setattr(account, "is_connected", lambda: True)

    def boom():
        raise RuntimeError("token revoked")
    monkeypatch.setattr(account, "sync_library", boom)
    webapp._refresh_library_if_stale()   # logs, doesn't raise


def test_not_connected_means_no_sync(monkeypatch):
    monkeypatch.setattr(account, "sync_library",
                        lambda: pytest.fail("must not sync"))
    webapp._refresh_library_if_stale()


def test_tests_never_use_a_real_login():
    assert not os.path.samefile(os.path.dirname(account.AUTH_FILE),
                                paths.DATA_DIR) \
        if os.path.exists(os.path.dirname(account.AUTH_FILE)) else True


# ---- 16. unwritable data folder -----------------------------------------------------------

def test_unwritable_data_folder_gives_a_clear_message(monkeypatch, capsys):
    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr("builtins.open", refuse)
    with pytest.raises(SystemExit) as stop:
        paths.check_data_dir_writable()
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert "can't write to its data folder" in err


def test_role_credits_are_not_offered_as_authors(client, monkeypatch):
    assert audible_api.is_role_credit("Stephen King - introduction")
    assert audible_api.is_role_credit("Matt Dinniman (Introduction By)")
    assert not audible_api.is_role_credit("Jean-Luc Picard")
    monkeypatch.setattr(mock_api, "CATALOG", mock_api.CATALOG + [
        make_product("B021", "Anthology", ["Ann Editor",
                                           "Test Author - introduction"],
                     "2021-01-01")])
    html = client.get("/search?q=Test+Author").get_data(as_text=True)
    assert "Test Author - introduction" not in html
