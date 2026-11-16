"""Sending through a mail relay on your own network: no sign-in, plain
SMTP. The SMTP classes are fakes; nothing is sent."""
import pytest

import checker
import db
from test_remaining_gaps import SETTINGS, smtp  # noqa: F401 - fixture

RELAY = dict(SETTINGS, smtp_host="mailrelay", smtp_port="25",
             smtp_security="none", smtp_user="", smtp_password="",
             email_from="watcher@home.example", email_to="me@example.com")


def test_relay_sends_without_login_or_tls(smtp):  # noqa: F811
    checker.send_email("S", "<p>x</p>", "x", RELAY)
    s = smtp[0]
    assert (s.host, s.port) == ("mailrelay", 25)
    assert "starttls" not in s.calls
    assert not any(c[0] == "login" for c in s.calls if isinstance(c, tuple))
    assert ("sendmail", "watcher@home.example", ["me@example.com"]) in s.calls


def test_relay_with_starttls_and_no_login(smtp):  # noqa: F811
    checker.send_email("S", "h", "t", dict(RELAY, smtp_security="starttls",
                                           smtp_port="587"))
    assert smtp[0].calls[0] == "starttls"
    assert not any(isinstance(c, tuple) and c[0] == "login"
                   for c in smtp[0].calls)


def test_relay_still_logs_in_when_credentials_are_given(smtp):  # noqa: F811
    checker.send_email("S", "h", "t", dict(RELAY, smtp_user="u",
                                           smtp_password="p"))
    assert ("login", "u", "p") in smtp[0].calls


@pytest.mark.parametrize("missing", ["email_from", "email_to"])
def test_relay_needs_from_and_to(smtp, missing):  # noqa: F811
    with pytest.raises(RuntimeError, match="not configured"):
        checker.send_email("S", "h", "t", dict(RELAY, **{missing: ""}))
    assert smtp == []


def test_email_ready():
    assert checker.email_ready(RELAY)
    assert not checker.email_ready(dict(RELAY, email_from=""))
    assert checker.email_ready(SETTINGS)
    assert not checker.email_ready(dict(SETTINGS, smtp_password=""))
    assert not checker.email_ready(dict(SETTINGS, smtp_host=""))


def test_settings_accept_relay_mode(client):
    client.post("/settings", data={
        "check_hour": "8", "smtp_host": "mailrelay", "smtp_port": "25",
        "smtp_security": "none", "smtp_user": "",
        "email_from": "watcher@home.example", "email_to": "me@example.com",
        "email_enabled": "on"})
    s = db.get_settings()
    assert s["smtp_security"] == "none" and s["email_enabled"] == "1"
    html = client.get("/settings").get_data(as_text=True)
    assert "sending to" in html and "mailrelay" in html
