"""Wishlist price alerts: reading the wishlist with prices, deciding when
to alert, the Wishlist tab and its settings. The audible package and SMTP
are replaced by fakes; nothing is sent to Amazon or a mail server."""
import pytest

import account
import app as webapp
import checker
import db
import wishlist
from mock_api import make_product


# ---------- fakes ----------

class FakeAuth:
    saved = 0

    def to_file(self, path, encryption=False, set_default=True, **kw):
        FakeAuth.saved += 1
        with open(path, "w") as f:
            f.write("{}")


def priced(asin, title, price, list_price=24.95, kind="sale",
           currency="USD"):
    p = make_product(asin, title, ["Some Author"], "2020-01-01")
    p["price"] = {"list_price": {"base": list_price,
                                 "currency_code": currency, "type": "list"},
                  "lowest_price": {"base": price, "currency_code": currency,
                                   "type": kind},
                  "credit_price": 1.0}
    return p


class FakeWishlistClient:
    """Serves a wishlist in pages. first_page=0 or 1 says how this fake
    counts pages; with 1, page 0 is answered like page 1 (as a 1-based API
    might), so the caller sees one repeated page."""

    def __init__(self, products, first_page=0, total=True, fail=False):
        self.products, self.first_page = products, first_page
        self.total, self.fail = total, fail
        self.pages = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, path, **params):
        assert path == "1.0/wishlist"
        assert "price" in params["response_groups"].split(",")
        if self.fail:
            raise ConnectionError("Amazon unavailable")
        page = params["page"]
        self.pages.append(page)
        index = max(page - self.first_page, 0)
        n = params["num_results"]
        data = {"products": self.products[index * n:(index + 1) * n]}
        if self.total:
            data["total_results"] = len(self.products)
        return data


@pytest.fixture
def amazon(monkeypatch, tmp_path):
    """A connected account whose wishlist the test controls."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    monkeypatch.setattr(account, "AUTH_FILE", str(auth_file))
    monkeypatch.setattr(account.audible.Authenticator, "from_file",
                        staticmethod(lambda path: FakeAuth()))
    state = {"client": FakeWishlistClient([])}
    monkeypatch.setattr(account.audible, "Client",
                        lambda auth: state["client"])
    FakeAuth.saved = 0

    def serve(products, **kw):
        state["client"] = FakeWishlistClient(products, **kw)
        return state["client"]
    return serve


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(checker, "send_email",
                        lambda subject, html, text, settings:
                        sent.append({"subject": subject, "html": html,
                                     "text": text}))
    db.save_settings({"email_enabled": "1"})
    return sent


# ---------- prices ----------

@pytest.mark.parametrize("raw, expected", [
    ("4.99", 4.99), ("$4.99", 4.99), ("4,99", 4.99), (" 5 ", 5.0),
    ({"base": 12.5}, 12.5), (7, 7.0), (0, 0.0),
    ("", None), ("abc", None), ("-1", None), (None, None), (True, None),
    ("nan", None), ("99999", None)])
def test_parse_amount(raw, expected):
    assert wishlist.parse_amount(raw) == expected


def test_parse_price_prefers_lowest_price():
    p = wishlist.parse_price(priced("A", "T", 3.99))
    assert p == {"price": 3.99, "list_price": 24.95, "price_type": "sale",
                 "currency": "USD"}


def test_parse_price_falls_back_to_list_price():
    prod = priced("A", "T", 3.99)
    del prod["price"]["lowest_price"]
    assert wishlist.parse_price(prod)["price"] == 24.95
    assert wishlist.parse_price(prod)["price_type"] == "list"


def test_parse_price_without_price_block():
    p = wishlist.parse_price(make_product("A", "T", ["X"], "2020-01-01"))
    assert p["price"] is None and p["list_price"] is None


def test_format_price():
    assert wishlist.format_price(4.5, "USD") == "$4.50"
    assert wishlist.format_price(4.5, "GBP") == "£4.50"
    assert wishlist.format_price(4.5, "XYZ") == "4.50 XYZ"
    assert wishlist.format_price(None) == "—"


# ---------- fetching ----------

@pytest.mark.parametrize("first_page", [0, 1])
@pytest.mark.parametrize("total", [True, False])
def test_fetch_reads_every_page(amazon, first_page, total):
    products = [priced(f"W{i:03}", f"Book {i}", 9.99) for i in range(120)]
    client = amazon(products, first_page=first_page, total=total)
    got = account.fetch_wishlist()
    assert [p["asin"] for p in got] == [p["asin"] for p in products]
    assert FakeAuth.saved == 1  # refreshed token kept
    assert len(client.pages) <= 4


def test_fetch_stops_on_a_short_first_page(amazon):
    client = amazon([priced("W1", "One", 5)])
    assert len(account.fetch_wishlist()) == 1
    assert client.pages == [0]


def test_failed_fetch_keeps_the_stored_wishlist(amazon):
    amazon([priced("W1", "One", 9.99)])
    wishlist.check()
    amazon([], fail=True)
    result = wishlist.check()
    assert "Amazon unavailable" in result["error"]
    assert [i["asin"] for i in db.wishlist_items()] == ["W1"]
    assert "Amazon unavailable" in db.get_settings()["wishlist_error"]


def test_not_connected_does_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(account, "AUTH_FILE", str(tmp_path / "none.json"))
    assert wishlist.check() == {"items": 0, "alerts": 0,
                                "email_sent": False, "error": None}


def test_refresh_keeps_targets_and_lowest_seen(amazon):
    amazon([priced("W1", "One", 9.99), priced("W2", "Two", 14.99)])
    wishlist.check()
    db.set_wishlist_target("W1", 7.0)
    amazon([priced("W1", "One", 12.99)])  # W2 left the wishlist
    wishlist.check()
    items = db.wishlist_items()
    assert [i["asin"] for i in items] == ["W1"]
    assert items[0]["target_price"] == 7.0
    assert items[0]["price"] == 12.99 and items[0]["lowest_seen"] == 9.99


def test_podcasts_are_skipped(amazon):
    pod = priced("P1", "A Podcast", 0)
    pod["content_type"] = "Podcast"
    amazon([pod, priced("W1", "One", 9.99)])
    wishlist.check()
    assert [i["asin"] for i in db.wishlist_items()] == ["W1"]


# ---------- alerting ----------

def test_alerts_at_or_below_default_price(amazon, mails):
    db.save_settings({"price_threshold": "5.00"})
    amazon([priced("W1", "Cheap", 4.99), priced("W2", "Exact", 5.00),
            priced("W3", "Dear", 5.01)])
    result = wishlist.check()
    assert result["alerts"] == 2 and result["email_sent"]
    assert len(mails) == 1
    assert "Cheap" in mails[0]["text"] and "Exact" in mails[0]["text"]
    assert "Dear" not in mails[0]["text"]
    assert "2 wishlist titles" in mails[0]["subject"]


def test_no_default_and_no_target_means_no_alerts(amazon, mails):
    amazon([priced("W1", "Free-ish", 0.99)])
    assert wishlist.check()["alerts"] == 0
    assert mails == []


def test_per_title_target_overrides_default(amazon, mails):
    db.save_settings({"price_threshold": "3.00"})
    amazon([priced("W1", "Wanted", 7.99), priced("W2", "Other", 7.99)])
    wishlist.check()
    assert mails == []
    db.set_wishlist_target("W1", 8.00)
    wishlist.check()
    assert len(mails) == 1 and "Wanted" in mails[0]["text"]
    assert "Other" not in mails[0]["text"]


def test_alert_once_then_only_when_cheaper(amazon, mails):
    db.save_settings({"price_threshold": "5"})
    amazon([priced("W1", "Deal", 4.99)])
    wishlist.check()
    wishlist.check()  # same price: no second email
    assert len(mails) == 1
    amazon([priced("W1", "Deal", 2.99)])
    wishlist.check()  # cheaper still: tell me
    assert len(mails) == 2 and "$2.99" in mails[1]["text"]


def test_alert_rearms_after_price_goes_back_up(amazon, mails):
    db.save_settings({"price_threshold": "5"})
    amazon([priced("W1", "Deal", 4.99)])
    wishlist.check()
    amazon([priced("W1", "Deal", 19.99)])
    wishlist.check()
    assert db.wishlist_items()[0]["alerted_price"] is None
    amazon([priced("W1", "Deal", 4.99)])
    wishlist.check()
    assert len(mails) == 2


def test_owned_titles_are_not_alerted(amazon, mails):
    db.save_settings({"price_threshold": "5"})
    db.replace_owned([{"asin": "W1", "title": "Bought it"}])
    amazon([priced("W1", "Bought it", 1.99)])
    assert wishlist.check()["alerts"] == 0
    assert mails == []


def test_email_off_marks_alerts_without_sending(amazon, mails):
    db.save_settings({"price_threshold": "5", "email_enabled": "0"})
    amazon([priced("W1", "Deal", 4.99)])
    result = wishlist.check()
    assert result["alerts"] == 1 and not result["email_sent"]
    assert mails == []
    db.save_settings({"email_enabled": "1"})
    wishlist.check()  # not saved up for the day email is switched on
    assert mails == []


def test_failed_email_is_retried_next_check(amazon, monkeypatch):
    db.save_settings({"price_threshold": "5", "email_enabled": "1"})
    amazon([priced("W1", "Deal", 4.99)])

    def broken(*a, **k):
        raise ConnectionError("mail server down")
    monkeypatch.setattr(checker, "send_email", broken)
    result = wishlist.check()
    assert "mail server down" in result["error"]
    assert db.wishlist_items()[0]["alerted_price"] is None
    sent = []
    monkeypatch.setattr(checker, "send_email",
                        lambda *a, **k: sent.append(a))
    assert wishlist.check()["email_sent"] and len(sent) == 1
    assert db.get_settings()["wishlist_error"] == ""


def test_new_target_rearms_the_alert(amazon, mails):
    db.save_settings({"price_threshold": "5"})
    amazon([priced("W1", "Deal", 4.99)])
    wishlist.check()
    db.set_wishlist_target("W1", 6.0)
    wishlist.check()
    assert len(mails) == 2


def test_email_is_escaped(amazon, mails):
    db.save_settings({"price_threshold": "5"})
    amazon([priced("W1", "Q&A <Live>", 4.99)])
    wishlist.check()
    assert "Q&amp;A &lt;Live&gt;" in mails[0]["html"]
    assert "<Live>" not in mails[0]["html"]


def test_disconnect_forgets_the_wishlist(amazon, monkeypatch):
    amazon([priced("W1", "One", 9.99)])
    wishlist.check()
    monkeypatch.setattr(FakeAuth, "deregister_device", lambda self: None,
                        raising=False)
    account.disconnect()
    assert db.wishlist_items() == []


# ---------- pages ----------

def test_wishlist_tab_without_account(client):
    html = client.get("/?tab=wishlist").get_data(as_text=True)
    assert "connect" in html and "your Audible account" in html


def test_wishlist_tab_lists_hits_first(client, amazon):
    db.save_settings({"price_threshold": "5"})
    amazon([priced("W1", "Pricey Book", 19.99),
            priced("W2", "Bargain Book", 3.99)])
    wishlist.check()
    html = client.get("/?tab=wishlist").get_data(as_text=True)
    assert html.index("Bargain Book") < html.index("Pricey Book")
    assert "$3.99" in html and "$24.95" in html
    assert "Default alert price: <strong>$5.00</strong>" in html


def test_set_and_clear_a_title_target(client, amazon):
    amazon([priced("W1", "One", 9.99)])
    wishlist.check()
    client.post("/wishlist/W1/target", data={"target": "$6.50"})
    assert db.wishlist_items()[0]["target_price"] == 6.5
    client.post("/wishlist/W1/target", data={"target": ""})
    assert db.wishlist_items()[0]["target_price"] is None


def test_bad_target_is_refused(client, amazon):
    amazon([priced("W1", "One", 9.99)])
    wishlist.check()
    resp = client.post("/wishlist/W1/target", data={"target": "cheap"},
                       follow_redirects=True)
    assert "must be an amount" in resp.get_data(as_text=True)
    assert db.wishlist_items()[0]["target_price"] is None


def test_target_for_unknown_title(client):
    resp = client.post("/wishlist/NOPE/target", data={"target": "3"},
                       follow_redirects=True)
    assert "on your wishlist any more" in resp.get_data(as_text=True)


def test_refresh_button(client, amazon):
    amazon([priced("W1", "One", 9.99), priced("W2", "Two", 9.99)])
    resp = client.post("/wishlist/refresh", follow_redirects=True)
    assert "Wishlist refreshed — 2 title(s)" in resp.get_data(as_text=True)


def test_refresh_needs_an_account(client):
    resp = client.post("/wishlist/refresh", follow_redirects=True)
    assert "Connect your Audible account first" in resp.get_data(as_text=True)


SETTINGS_FORM = {"check_hour": "8", "smtp_host": "smtp.gmail.com",
                 "smtp_port": "465", "smtp_security": "ssl",
                 "smtp_user": "", "marketplace": "us", "language": ""}


def test_settings_save_default_price(client):
    client.post("/settings", data={**SETTINGS_FORM, "price_threshold": "$5"})
    assert db.get_settings()["price_threshold"] == "5.00"
    client.post("/settings", data={**SETTINGS_FORM, "price_threshold": ""})
    assert db.get_settings()["price_threshold"] == ""


def test_settings_refuse_bad_price(client):
    resp = client.post("/settings", data={**SETTINGS_FORM,
                                          "price_threshold": "lots"},
                       follow_redirects=True)
    assert "Wishlist alert price must be" in resp.get_data(as_text=True)
    assert db.get_settings()["price_threshold"] == ""


def test_settings_form_without_the_field_keeps_it(client):
    db.save_settings({"price_threshold": "4.00"})
    client.post("/settings", data=SETTINGS_FORM)
    assert db.get_settings()["price_threshold"] == "4.00"


def test_changing_default_price_rearms_alerts(client, amazon, mails):
    db.save_settings({"price_threshold": "5.00"})
    amazon([priced("W1", "Deal", 4.99)])
    wishlist.check()
    client.post("/settings", data={**SETTINGS_FORM, "price_threshold": "6",
                                   "email_enabled": "on"})
    wishlist.check()
    assert len(mails) == 2


def test_price_check_is_scheduled_twelve_hours_after_daily(client):
    client.post("/settings", data={**SETTINGS_FORM, "check_hour": "8"})
    job = webapp.scheduler.get_job("price_check")
    assert job is not None and "hour='20'" in str(job.trigger)
    client.post("/settings", data={**SETTINGS_FORM, "check_hour": "15"})
    assert "hour='3'" in str(webapp.scheduler.get_job("price_check").trigger)


def test_daily_check_also_checks_prices(amazon, mails, monkeypatch):
    db.save_settings({"price_threshold": "5"})
    amazon([priced("W1", "Deal", 4.99)])
    monkeypatch.setattr(webapp, "_refresh_library_if_stale", lambda: None)
    webapp._check_lock.acquire()
    webapp._run_check("scheduled")
    assert any("wishlist" in m["subject"] for m in mails)
    assert not webapp._check_lock.locked()
