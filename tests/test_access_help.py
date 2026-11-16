"""The user-facing help for access settings: the Settings panel, the
"address not recognised" page, and the README steps."""
import os

import app as webapp


def _panel(html):
    return html[html.index('id="access"'):]


def test_settings_panel_default_install(client):
    panel = _panel(client.get("/settings").get_data(as_text=True))
    assert "This computer only" in panel          # AW_HOST default
    assert "<code>localhost</code> ✅ allowed" in panel
    assert "add it to\n          <code>AW_ALLOWED_HOSTS</code>" in panel
    assert "not needed while only this" in panel   # password off, local
    assert webapp.README_ACCESS_URL in panel
    assert "<input" not in panel and "<form" not in panel  # read-only


def test_settings_panel_network_install(client, monkeypatch):
    monkeypatch.setattr(webapp, "HOST", "0.0.0.0")
    monkeypatch.setenv("AW_ALLOWED_HOSTS", "nas.example.com, Audible.Example.NET")
    panel = _panel(client.get("/settings").get_data(as_text=True))
    assert "Other devices on your network" in panel
    assert "<code>nas.example.com</code>, <code>audible.example.net</code>" in panel
    assert "anyone who can reach this address" in panel  # warns: no password


def test_settings_panel_password_on(client, monkeypatch):
    monkeypatch.setenv("AW_PASSWORD", "pw")
    with client.session_transaction() as s:
        s["signed_in"] = webapp._password_tag()
    panel = _panel(client.get("/settings").get_data(as_text=True))
    assert "🔒 On" in panel and "pw" not in panel.split("🔒 On")[1][:200]


def test_refused_page_gives_exact_steps(client):
    resp = client.get("/", headers={"Host": "nas.example.com:5055"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 400
    assert "Address not recognised" in html
    assert "- AW_ALLOWED_HOSTS=nas.example.com" in html          # Compose
    assert "-e AW_ALLOWED_HOSTS=nas.example.com" in html            # docker run
    assert webapp.README_ACCESS_URL in html


def test_refused_page_escapes_the_hostname(client):
    resp = client.get("/", headers={"Host": "<script>x</script>.example.com"})
    html = resp.get_data(as_text=True)
    assert "<script>x</script>" not in html
    # Werkzeug drops a hostname like that; the page shows a placeholder
    assert "AW_ALLOWED_HOSTS=your-server-name" in html


def test_readme_access_section():
    readme = open(os.path.join(os.path.dirname(webapp.__file__), "README.md"),
                  encoding="utf-8").read()
    section = readme[readme.index("## Opening it from other devices"):
                     readme.index("## Notes")]
    for heading in ("### Docker Compose", "### docker run"):
        assert heading in section
    for var in ("AW_ALLOWED_HOSTS", "AW_PASSWORD"):
        assert var in section
    # the anchor the app links to exists
    assert webapp.README_ACCESS_URL.endswith("#opening-it-from-other-devices")
