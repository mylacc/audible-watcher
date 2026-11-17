"""Series tab: sort A–Z (default) or by the newest already-released title,
so a release you may have missed rises to the top."""
from datetime import date, timedelta

import db

DAY = timedelta(days=1)


def _series(name, *release_dates):
    wid = db.add_watch("series", name, None)
    db.upsert_books(wid, [{"asin": f"{name[:3].upper()}{i}", "title": f"{name} {i}",
                           "authors": "A", "url": "u", "release_date": d}
                          for i, d in enumerate(release_dates)], as_new=False)
    return wid


def _order(html):
    names = ["Alpha", "Bravo", "Charlie", "Delta"]
    table = html[html.index('class="watch-table"'):]
    return sorted(names, key=lambda n: table.find(f">{n}</a>"))


def test_series_sort_by_last_released(client):
    today = date.today()
    _series("Alpha", (today - 400 * DAY).isoformat())
    # newest title out a week ago, plus a pre-order: the pre-order mustn't count
    _series("Bravo", (today - 7 * DAY).isoformat(), (today + 30 * DAY).isoformat())
    _series("Charlie", (today - 2 * DAY).isoformat())
    _series("Delta", (today + 60 * DAY).isoformat())       # nothing out yet

    html = client.get("/?tab=series").get_data(as_text=True)
    assert _order(html) == ["Alpha", "Bravo", "Charlie", "Delta"]   # A–Z default
    assert "<th>Latest release</th>" in html

    html = client.get("/?tab=series&order=released").get_data(as_text=True)
    assert _order(html) == ["Charlie", "Bravo", "Alpha", "Delta"]
    assert "<th>Last released</th>" in html
    # the column shows the last title already out, not Bravo's pre-order
    assert (today - 7 * DAY).isoformat() in html
    assert (today + 30 * DAY).isoformat() not in html


def test_unknown_series_order_falls_back_to_a_to_z(client):
    _series("Bravo", "2020-01-01")
    _series("Alpha", "2010-01-01")
    html = client.get("/?tab=series&order=bogus").get_data(as_text=True)
    table = html[html.index('class="watch-table"'):]
    assert table.find(">Alpha</a>") < table.find(">Bravo</a>")
