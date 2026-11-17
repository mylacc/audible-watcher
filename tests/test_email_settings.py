"""Email section of the Settings page: mail relay setup, status, friendly
errors."""
import smtplib
import socket
import ssl

import checker
import db

RELAY = {"check_hour": "8", "smtp_host": "192.0.2.10", "smtp_port": "25",
         "smtp_security": "none", "smtp_user": "",
         "email_from": "alerts@example.com", "email_to": "me@example.com"}


def test_unconfigured_page_shows_relay_steps(client):
    html = client.get("/settings").get_data(as_text=True)
    assert "Not set up yet" in html
    # the relay steps start expanded when email isn't set up
    assert '<details class="help-box" open>' in html
    assert "How to point it at your mail relay" in html
    assert "Troubleshooting" in html
    # relay only: no mail-provider sign-up guides or presets
    lower = html.lower()
    for gone in ("gmail", "myaccount.google.com", "app password",
                 "yahoo", "icloud", "fastmail", "zoho", "js-preset"):
        assert gone not in lower, gone


def test_defaults_are_a_plain_relay(client):
    s = db.get_settings()
    assert (s["smtp_host"], s["smtp_port"], s["smtp_security"]) == ("", "25", "none")
    html = client.get("/settings").get_data(as_text=True)
    assert '<option value="none" selected>' in html


def test_configured_page_shows_status(client):
    client.post("/settings", data={**RELAY, "email_enabled": "on"})
    html = client.get("/settings").get_data(as_text=True)
    assert "sending to" in html and "me@example.com" in html
    assert "via 192.0.2.10" in html
    assert '<details class="help-box" open>' not in html  # steps collapsed
    assert '<details class="help-box relay-login" >' in html  # no login: closed


def test_relay_login_box_opens_when_a_login_is_saved(client):
    client.post("/settings", data={**RELAY, "smtp_user": "u",
                                   "smtp_password": "p"})
    html = client.get("/settings").get_data(as_text=True)
    assert '<details class="help-box relay-login" open>' in html


def test_password_keeps_inner_spaces(client):
    client.post("/settings", data={**RELAY, "smtp_user": "u",
                                   "smtp_password": "  pass phrase  "})
    assert db.get_settings()["smtp_password"] == "pass phrase"


def test_enabling_email_without_from_warns(client):
    resp = client.post("/settings", data={**RELAY, "email_from": "",
                                          "email_enabled": "on"},
                       follow_redirects=True)
    assert "isn&#39;t fully set up" in resp.get_data(as_text=True)


def test_test_email_failure_explains_the_fix(client, monkeypatch):
    def reject(*a, **k):
        raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")
    monkeypatch.setattr(checker, "send_email", reject)
    resp = client.post("/test-email", follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "Test email failed" in html and "usually need no login" in html


def test_explain_smtp_error_hints():
    assert "None" in checker.explain_smtp_error(
        ssl.SSLError("WRONG_VERSION_NUMBER"))
    assert "IP address" in checker.explain_smtp_error(
        socket.gaierror("Name or service not known"))
    assert "localhost" in checker.explain_smtp_error(
        ConnectionRefusedError("refused"))
    assert "From address" in checker.explain_smtp_error(
        smtplib.SMTPSenderRefused(553, b"Sender address rejected", "x@y"))
    assert "network" in checker.explain_smtp_error(
        smtplib.SMTPRecipientsRefused({"me@example.com": (554, b"Relay access denied")}))
    assert checker.explain_smtp_error(ValueError("odd")) == "odd"


def test_settings_page_is_one_set_of_cards(client):
    html = client.get("/settings").get_data(as_text=True)
    main = html[html.index("<main>"):]
    titles = [main[i + len('class="panel-title">'):main.index("<", i + 20)]
              for i in range(len(main)) if main.startswith('class="panel-title">', i)]
    assert titles == ["Audible account", "Monitoring", "Email notifications",
                      "Access from other devices"]
    assert "<fieldset" not in main
    email = main[main.index('id="email"'):main.index('class="save-bar"')]
    assert 'formaction="/test-email"' in email          # test button inside
    assert "Troubleshooting" in email
    # Enter saves: the form's first submit button is Save, not the test email
    form = main[main.index('class="settings-form"'):]
    first = form[form.index("<button"):form.index("</button>")]
    assert "Save settings" in first and "formaction" not in first


def test_test_email_button_posts_from_the_settings_form(client, monkeypatch):
    sent = []
    monkeypatch.setattr(checker, "send_test_email", lambda s: sent.append(s))
    resp = client.post("/test-email", data={"smtp_host": "ignored",
                                            "check_hour": "8"})
    assert resp.status_code == 302 and len(sent) == 1
    assert sent[0]["smtp_host"] == db.get_settings()["smtp_host"]  # saved ones


def test_favicon_on_every_page(client):
    for path in ("/", "/settings", "/search"):
        html = client.get(path).get_data(as_text=True)
        assert 'rel="icon" type="image/svg+xml" href="/static/favicon.svg"' in html
        assert 'rel="apple-touch-icon" href="/static/apple-touch-icon.png"' in html
    for f, kind in (("favicon.svg", "image/svg+xml"), ("favicon-32.png", "image/png"),
                    ("apple-touch-icon.png", "image/png")):
        resp = client.get(f"/static/{f}")
        assert resp.status_code == 200 and resp.mimetype == kind
