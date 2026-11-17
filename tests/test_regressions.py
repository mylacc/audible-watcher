"""Regression tests for the four high-severity issues from the 2026-11 review.
Each test failed on v1.0.0 and passes with the fix."""
import re

import pytest

import app as webapp
import audible_api
import checker
import db
import mock_api


# ---- 1. a blank/invalid check hour must not break the app ----------------

@pytest.mark.parametrize("bad", ["", "abc", "24", "-1"])
def test_invalid_check_hour_is_rejected(client, bad):
    resp = client.post("/settings", data={"check_hour": bad,
                                          "marketplace": "us"})
    assert resp.status_code == 302
    assert db.get_settings()["check_hour"] == "8", "bad value must not be saved"


def test_bad_stored_check_hour_does_not_stop_startup():
    """A database that already holds a bad hour (saved by v1.0.0) must still
    let the app schedule its check and start."""
    db.save_settings({"check_hour": ""})
    webapp.reschedule()  # raised ValueError before the fix
    job = webapp.scheduler.get_job("daily_check")
    assert job is not None and "hour='8'" in str(job.trigger)


# ---- 2. cross-site request forgery / password theft ------------------

def test_post_without_csrf_token_is_rejected(raw_client):
    before = db.get_settings()["smtp_host"]
    resp = raw_client.post("/settings", data={"check_hour": "8",
                                              "smtp_host": "evil.example"})
    # refused: nothing changes; the browser is sent back with a note
    assert resp.status_code == 302
    assert db.get_settings()["smtp_host"] == before
    sent = []
    import checker
    checker_send = checker.send_test_email
    checker.send_test_email = lambda s: sent.append(s)
    try:
        assert raw_client.post("/test-email").status_code == 302
    finally:
        checker.send_test_email = checker_send
    assert sent == []


def test_fetch_post_without_csrf_token_gets_json_error(raw_client):
    resp = raw_client.post("/watch/add", data={"type": "author",
                                               "name": "Test Author"},
                           headers={"X-Requested-With": "fetch"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert db.list_watches() == []


def test_wrong_csrf_token_is_rejected(client, watched):
    mock_api.state["include_new"] = True
    checker.check_all()
    resp = client.post("/seen-all", data={"csrf_token": "forged"},
                       follow_redirects=True)
    assert "Nothing was" in resp.get_data(as_text=True)
    assert db.new_books(), "the forged 'mark all seen' must not apply"


def test_refused_post_never_redirects_offsite(raw_client):
    resp = raw_client.post("/seen-all",
                           headers={"Referer": "https://evil.example/x"})
    assert resp.status_code == 302
    assert "evil.example" not in resp.location


def test_changing_smtp_server_requires_password_again(client):
    """Even with a valid token (e.g. DNS rebinding), the saved password
    must never be sent to a server the user didn't enter it for."""
    db.save_settings({"smtp_host": "relay.example", "smtp_user": "me@x",
                      "smtp_password": "secret-pw"})
    client.post("/settings", data={"check_hour": "8",
                                   "smtp_host": "evil.example",
                                   "smtp_user": "me@x", "smtp_password": ""})
    assert db.get_settings()["smtp_password"] == ""
    # an unchanged server keeps the saved password
    db.save_settings({"smtp_host": "relay.example",
                      "smtp_password": "secret-pw"})
    client.post("/settings", data={"check_hour": "8",
                                   "smtp_host": "relay.example",
                                   "smtp_user": "me@x", "smtp_password": ""})
    assert db.get_settings()["smtp_password"] == "secret-pw"


def test_every_post_form_carries_a_csrf_token(client, watched):
    db.replace_owned([{"asin": "B001", "title": "The First Book",
                       "authors": "Test Author", "url": "u"}])
    mock_api.state["include_new"] = True
    checker.check_all()  # NEW books -> "Mark seen" forms
    pages = ["/", "/?tab=series", "/?tab=preorders", "/?tab=library",
             "/?tab=library&view=author", "/search?q=Test+Author",
             "/search?q=Great+Series", "/settings"]
    pages += [f"/watch/{w['id']}" for w in db.list_watches()]
    for p in pages:
        html = client.get(p).get_data(as_text=True)
        for form in re.findall(r'<form[^>]*method="post".*?</form>', html,
                               flags=re.S):
            assert 'name="csrf_token"' in form, (p, form[:120])


def test_secret_key_is_not_the_public_default():
    assert webapp.app.secret_key != "audible-watcher-local"
    assert len(webapp.app.secret_key) >= 32


# ---- 3. placeholder cleanup must not hide other series' books ------------

def test_placeholder_cleanup_ignores_same_number_in_other_series(watched):
    aid = watched["author"]
    other = {"asin": "B010", "title": "Other Series Book One",
             "authors": "Test Author", "series_title": "Other Series",
             "series_sequence": "1", "release_date": None,
             "url": "u"}
    db.upsert_books(aid, [other], as_new=False)
    # an undated book 1 of *another* series isn't a placeholder of B001
    assert db.hide_placeholders(aid) == 0
    visible = {b["asin"] for b in db.books_for_watch(aid)}
    assert {"B001", "B010"} <= visible


# ---- 4. region editions must not be emailed as new -----------------------

@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(checker, "send_new_release_email",
                        lambda books, settings: sent.append(books))
    db.save_settings({"email_enabled": "1"})
    return sent


def test_foreign_region_edition_is_not_emailed(watched, outbox, monkeypatch):
    monkeypatch.setattr(
        checker.audible_api, "check_market_availability",
        lambda asin, title, author=None, marketplace="us": asin != "B004")
    mock_api.state["include_new"] = True
    checker.check_all()
    assert not any(b["asin"] == "B004" for batch in outbox for b in batch)
    assert not any(b["asin"] == "B004" for b in db.unnotified_books())
    for wid in (watched["author"], watched["series"]):
        assert "B004" not in {b["asin"] for b in db.books_for_watch(wid)}


def test_undated_release_waits_until_it_can_be_verified(watched, outbox,
                                                        monkeypatch):
    monkeypatch.setitem(mock_api.NEW_RELEASE, "release_date", None)
    mock_api.state["include_new"] = True
    checker.check_all()
    assert outbox == [], "TBA editions can't be region-checked yet"
    assert {b["asin"] for b in db.unnotified_books()} == {"B004"}
    monkeypatch.setitem(mock_api.NEW_RELEASE, "release_date", "2099-10-20")
    checker.check_all()  # now dated -> verified -> emailed once
    assert [b["asin"] for b in outbox[0]] == ["B004"]
    assert db.unnotified_books() == []


def test_availability_lookup_failure_defers_email(watched, outbox,
                                                  monkeypatch):
    api_up = False

    def lookup(asin, title, author=None, marketplace="us"):
        if not api_up:
            raise audible_api.AudibleAPIError("HTTP 503")
        return True
    monkeypatch.setattr(checker.audible_api, "check_market_availability",
                        lookup)
    mock_api.state["include_new"] = True
    checker.check_all()
    assert outbox == []
    assert {b["asin"] for b in db.unnotified_books()} == {"B004"}
    api_up = True
    checker.check_all()  # retried on the next check
    assert [b["asin"] for b in outbox[0]] == ["B004"]
