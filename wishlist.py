"""Wishlist price alerts: fetch the connected account's Audible wishlist
with prices, and email when a title is at or below its alert price.

Each title's alert price is its own target if one is set on the Wishlist
tab, otherwise the default from Settings (blank = no default). A title is
reported once when it reaches its alert price, again only if it gets
cheaper still, and the alert re-arms once its price goes back above it.
"""
from __future__ import annotations

import html
import logging

import account
import audible_api
import checker
import db

log = logging.getLogger(__name__)

CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$",
                    "AUD": "A$", "INR": "₹", "JPY": "¥"}
MARKET_CURRENCY = {"us": "USD", "uk": "GBP", "ca": "CAD", "au": "AUD",
                   "de": "EUR", "fr": "EUR", "it": "EUR", "es": "EUR",
                   "in": "INR", "jp": "JPY"}
CENT = 0.005  # prices are compared to the cent
MAX_PRICE = 10000


def parse_amount(value) -> float | None:
    """A price as a positive-or-zero float, from Audible's {"base": 9.99}
    objects, plain numbers, or what someone typed ("$4.99", "4,99")."""
    if isinstance(value, dict):
        value = value.get("base")
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().lstrip("$£€¥₹").replace(",", ".").strip()
        if not value:
            return None
    try:
        amount = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    if amount != amount or amount < 0 or amount > MAX_PRICE:  # NaN, nonsense
        return None
    return amount


def parse_price(product: dict) -> dict:
    """What a title costs now. Audible's "lowest_price" is the price you'd
    pay (a sale or member price when there is one); fall back to the list
    price when it's missing."""
    block = product.get("price") or {}
    lowest = block.get("lowest_price") or {}
    listed = block.get("list_price") or {}
    if not isinstance(lowest, dict):
        lowest = {"base": lowest}
    if not isinstance(listed, dict):
        listed = {"base": listed}
    price = parse_amount(lowest)
    kind = lowest.get("type")
    if price is None:
        price = parse_amount(listed)
        kind = "list" if price is not None else None
    return {"price": price, "list_price": parse_amount(listed),
            "price_type": kind,
            "currency": lowest.get("currency_code")
            or listed.get("currency_code")}


def market_currency(settings: dict | None = None) -> str:
    settings = settings if settings is not None else db.get_settings()
    return MARKET_CURRENCY.get(settings.get("marketplace", "us"), "USD")


def format_price(amount, currency: str | None = None) -> str:
    if amount is None:
        return "—"
    currency = currency or market_currency()
    symbol = CURRENCY_SYMBOLS.get(currency)
    if symbol:
        return f"{symbol}{amount:,.2f}"
    return f"{amount:,.2f} {currency}"


def default_target(settings: dict | None = None) -> float | None:
    settings = settings if settings is not None else db.get_settings()
    return parse_amount(settings.get("price_threshold"))


def effective_target(item: dict, default: float | None) -> float | None:
    if item.get("target_price") is not None:
        return item["target_price"]
    return default


def at_or_below(item: dict, default: float | None) -> bool:
    target = effective_target(item, default)
    return (target is not None and item.get("price") is not None
            and item["price"] <= target + CENT)


def refresh() -> int:
    """Fetch the wishlist into the database; returns the title count."""
    marketplace = db.get_settings().get("marketplace", "us")
    rows = []
    for product in account.fetch_wishlist():
        b = audible_api._simplify(product, marketplace)
        if audible_api.is_podcast(b):
            continue
        b.update(parse_price(product))
        rows.append(b)
    db.replace_wishlist(rows)
    return len(rows)


def due_alerts(default: float | None) -> tuple[list[dict], list[str]]:
    """(titles to report now, titles whose alert re-arms). Owned titles
    are skipped: Audible usually drops them from the wishlist anyway."""
    owned = db.owned_asins()
    due, rearm = [], []
    for item in db.wishlist_items():
        if item["asin"] in owned:
            continue
        if at_or_below(item, default):
            last = item.get("alerted_price")
            if last is None or item["price"] < last - CENT:
                item["target"] = effective_target(item, default)
                due.append(item)
        elif item.get("alerted_price") is not None:
            rearm.append(item["asin"])
    return due, rearm


def check(trigger: str = "manual") -> dict:
    """Refresh the wishlist and send one email for titles that reached
    their alert price. Never raises: the outcome is stored for the
    Wishlist tab and returned."""
    result = {"items": 0, "alerts": 0, "email_sent": False, "error": None}
    if not account.is_connected():
        return result
    settings = db.get_settings()
    try:
        result["items"] = refresh()
    except Exception as e:  # noqa: BLE001 - shown on the Wishlist tab
        log.warning("Wishlist fetch failed (%s)", trigger, exc_info=True)
        result["error"] = f"Couldn't read the wishlist: {e}"
        _record(result)
        return result

    due, rearm = due_alerts(default_target(settings))
    db.rearm_price_alerts(rearm)
    result["alerts"] = len(due)
    if due:
        if settings.get("email_enabled") == "1":
            try:
                send_price_email(due, settings)
                result["email_sent"] = True
            except Exception as e:  # noqa: BLE001
                log.exception("Price alert email failed")
                result["error"] = ("Price alert email failed: "
                                   + checker.explain_smtp_error(e))
                _record(result)
                return result  # not marked: the next check tries again
        # with email off they're on the Wishlist tab; don't save them up
        db.mark_price_alerted({i["asin"]: i["price"] for i in due})
    log.info("Wishlist (%s): %d title(s), %d at or below target", trigger,
             result["items"], result["alerts"])
    _record(result)
    return result


def _record(result: dict):
    db.save_settings({"wishlist_checked_at": db.now_iso(),
                      "wishlist_error": result["error"] or ""})


# ---------- email ----------

def _row_html(i: dict) -> str:
    e = html.escape
    img = (f'<img src="{e(i["image_url"])}" width="80" alt="" '
           'style="border-radius:6px;vertical-align:top">'
           if i.get("image_url") else "")
    was = ""
    if i.get("list_price") and i["list_price"] > i["price"] + CENT:
        was = (f' <span style="color:#999;text-decoration:line-through">'
               f'{e(format_price(i["list_price"], i.get("currency")))}</span>')
    kind = (f' <span style="color:#666">({e(i["price_type"])} price)</span>'
            if i.get("price_type") and i["price_type"] != "list" else "")
    return f"""
    <tr>
      <td style="padding:10px 12px 10px 0">{img}</td>
      <td style="padding:10px 0">
        <a href="{e(i.get('url') or '')}" style="font-size:16px;
           font-weight:bold;color:#f29d38;text-decoration:none">{e(i.get('title') or '')}</a><br>
        by {e(i.get('authors') or 'Unknown')}<br>
        <strong style="font-size:16px">{e(format_price(i['price'], i.get('currency')))}</strong>{was}{kind}<br>
        <span style="color:#999;font-size:12px">Your alert price:
        {e(format_price(i['target'], i.get('currency')))}</span>
      </td>
    </tr>"""


def build_price_email(items: list[dict]) -> tuple[str, str, str]:
    n = len(items)
    s = "s" if n != 1 else ""
    subject = (f"Audible Watcher: {n} wishlist title{s} at or below "
               "your price")
    rows = "\n".join(_row_html(i) for i in items)
    body = f"""<html><body style="font-family:Arial,Helvetica,sans-serif;
      color:#222;max-width:640px">
      <h2 style="color:#f29d38">🏷️ Wishlist price alert</h2>
      <p>{n} title{s} on your Audible wishlist {'are' if n != 1 else 'is'}
      at or below your alert price. Sale prices can end without notice.</p>
      <table cellspacing="0" cellpadding="0">{rows}</table>
      <p style="color:#999;font-size:12px;margin-top:24px">
        Sent by your Audible Watcher app.</p>
    </body></html>"""
    lines = [f"{n} wishlist title{s} at or below your alert price:", ""]
    for i in items:
        lines.append(f"- {i.get('title')} by {i.get('authors') or 'Unknown'}"
                     f" — {format_price(i['price'], i.get('currency'))}"
                     f" (alert price "
                     f"{format_price(i['target'], i.get('currency'))})")
        lines.append(f"  {i.get('url') or ''}")
    return subject, body, "\n".join(lines)


def send_price_email(items: list[dict], settings: dict):
    subject, body, text = build_price_email(items)
    checker.send_email(subject, body, text, settings)
