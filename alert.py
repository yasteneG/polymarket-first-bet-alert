#!/usr/bin/env python3
"""
Alerte Polymarket : nouveau compte dont le PREMIER pari dépasse un seuil (10 000 $ par défaut).

Fonctionnement à chaque exécution :
  1. Récupère les achats >= MIN_FILL_USD des LOOKBACK_MIN dernières minutes.
  2. Additionne les morceaux par (compte, marché) : un gros ordre est souvent exécuté en plusieurs fois.
  3. Pour chaque candidat, lit l'historique complet du compte (du plus ancien au plus récent) et vérifie
     que son tout premier trade est sur ce marché, récent, et que ses achats sur ce marché pendant
     FIRST_BET_WINDOW_MIN minutes dépassent THRESHOLD_USD.
  4. Envoie une alerte Telegram (ou l'affiche dans la console si Telegram n'est pas configuré).

Uniquement la bibliothèque standard Python : aucune dépendance à installer.
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Paris"))
except Exception:
    TZ = timezone.utc

API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

THRESHOLD_USD = float(os.getenv("THRESHOLD_USD", "10000"))
MIN_FILL_USD = float(os.getenv("MIN_FILL_USD", "1000"))
LOOKBACK_MIN = int(os.getenv("LOOKBACK_MIN", "60"))
FIRST_BET_WINDOW_MIN = int(os.getenv("FIRST_BET_WINDOW_MIN", "30"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# Renvoi manuel : renvoie l'alerte de ce compte, même ancienne ou déjà envoyée.
RESEND_WALLET = os.getenv("RESEND_WALLET", "").strip().lower()

# Comptes déjà identifiés comme anciens : on les garde 30 jours pour éviter de les revérifier.
OLD_WALLET_TTL = 30 * 24 * 3600
ALERTED_TTL = 7 * 24 * 3600


def get_json(path, params, retries=3, base=API):
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymarket-first-bet-alert/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt * (5 if getattr(e, "code", None) == 429 else 1)
            print(f"  erreur API ({e}), nouvel essai dans {wait}s", file=sys.stderr)
            time.sleep(wait)


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("old", {})
    state.setdefault("alerted", {})
    return state


def save_state(state, now):
    state["old"] = {w: t for w, t in state["old"].items() if now - t < OLD_WALLET_TTL}
    state["alerted"] = {w: t for w, t in state["alerted"].items() if now - t < ALERTED_TTL}
    state["last_run"] = now
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_recent_trades(since_ts):
    """Achats >= MIN_FILL_USD depuis since_ts, en paginant (l'API renvoie du plus récent au plus ancien)."""
    trades, offset, page = [], 0, 1000
    while True:
        batch = get_json("/trades", {
            "limit": page, "offset": offset, "takerOnly": "true",
            "filterType": "CASH", "filterAmount": int(MIN_FILL_USD),
        })
        if not batch:
            break
        trades.extend(t for t in batch if t["timestamp"] >= since_ts)
        if batch[-1]["timestamp"] < since_ts or len(batch) < page or offset >= 10000:
            break
        offset += page
    return trades


def find_candidates(trades):
    """(compte, marché) dont les achats cumulés approchent le seuil."""
    totals = defaultdict(float)
    for t in trades:
        if t["side"] == "BUY":
            totals[(t["proxyWallet"], t["conditionId"])] += t["size"] * t["price"]
    # Marge de 20 % : les morceaux < MIN_FILL_USD ne sont pas vus ici, le vrai total est recalculé ensuite.
    return {w for (w, _), usd in totals.items() if usd >= THRESHOLD_USD * 0.8}


def check_first_bet(wallet, now, force=False):
    """
    Renvoie ("alert", infos), ("old", None) si le compte a déjà un historique,
    ou ("pending", None) si c'est trop tôt pour conclure.
    """
    activity = get_json("/activity", {
        "user": wallet, "type": "TRADE", "limit": 200,
        "sortBy": "TIMESTAMP", "sortDirection": "ASC",
    })
    if not activity:
        return "pending", None

    first = activity[0]
    if not force and now - first["timestamp"] > (LOOKBACK_MIN + 15) * 60:
        return "old", None

    window_end = first["timestamp"] + FIRST_BET_WINDOW_MIN * 60
    market = first["conditionId"]
    buys = [a for a in activity
            if a["conditionId"] == market and a["side"] == "BUY" and a["timestamp"] <= window_end]
    total_usd = sum(float(a.get("usdcSize") or 0) for a in buys)
    total_shares = sum(float(a.get("size") or 0) for a in buys)

    if total_usd >= THRESHOLD_USD:
        by_outcome = defaultdict(float)
        for a in buys:
            by_outcome[a.get("outcome", "?")] += float(a.get("usdcSize") or 0)
        return "alert", {
            "wallet": wallet,
            "name": first.get("name") or first.get("pseudonym") or "",
            "title": first.get("title", ""),
            "event_slug": first.get("eventSlug") or first.get("slug") or "",
            "outcome": max(by_outcome, key=by_outcome.get),
            "usd": total_usd,
            "shares": total_shares,
            "avg_price": total_usd / total_shares if total_shares else 0,
            "fills": len(buys),
            "condition_id": market,
            "first_ts": first["timestamp"],
            "last_ts": buys[-1]["timestamp"],
            "tx": first.get("transactionHash", ""),
        }
    if now > window_end and not force:
        return "old", None
    return "pending", None


def parse_date(value):
    """Dates Gamma : '2026-11-04T04:59:00Z' ou '2026-10-29 02:30:00+00'."""
    if not value:
        return None
    v = value.strip().replace(" ", "T").replace("Z", "+00:00")
    if len(v) >= 3 and v[-3] in "+-" and v[-2:].isdigit():
        v += ":00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def market_dates(condition_id):
    """(début du match, fin du marché) ; None si inconnu."""
    try:
        markets = get_json("/markets", {"condition_ids": condition_id}, base=GAMMA_API)
    except Exception as e:
        print(f"  dates du marché indisponibles ({e})", file=sys.stderr)
        return None, None
    if not markets:
        return None, None
    m = markets[0]
    return parse_date(m.get("gameStartTime")), parse_date(m.get("endDate"))


def fmt_time(dt):
    return dt.astimezone(TZ).strftime("%d/%m à %H:%M")


def until(dt, now, past="déjà passé"):
    secs = (dt - now).total_seconds()
    if secs < 0:
        return past
    hours = secs / 3600
    if hours < 1:
        return f"dans {max(1, round(secs / 60))} min"
    if hours < 48:
        return f"dans {hours:.0f} h" if hours >= 10 else f"dans {int(hours)} h {int(secs % 3600 // 60):02d}"
    return f"dans {hours / 24:.0f} jours"


def usd(n):
    return f"{n:,.0f}".replace(",", " ")


def format_alert(a):
    e = html.escape
    now = datetime.now(timezone.utc)
    bet_first = datetime.fromtimestamp(a["first_ts"], timezone.utc)
    bet_last = datetime.fromtimestamp(a["last_ts"], timezone.utc)
    if bet_last - bet_first >= timedelta(minutes=1):
        bet_time = f"{fmt_time(bet_first)} → {bet_last.astimezone(TZ):%H:%M}"
    else:
        bet_time = fmt_time(bet_first)
    tz_name = "heure de Paris" if str(TZ) == "Europe/Paris" else str(TZ)
    game_start, market_end = market_dates(a["condition_id"])
    profile = f"https://polymarket.com/profile/{a['wallet']}"
    market = f"https://polymarket.com/event/{a['event_slug']}"
    short = f"{a['wallet'][:6]}…{a['wallet'][-4:]}"
    lines = [
        f"🚨 <b>Nouveau compte, premier pari ≥ {usd(THRESHOLD_USD)} $</b>",
        "",
        f"💰 <b>{usd(a['usd'])} $</b> sur <b>{e(a['outcome'])}</b>",
        f"📊 {e(a['title'])}",
        f"🎯 Prix moyen : {a['avg_price']:.3f} (≈ {a['avg_price'] * 100:.1f} %)",
        f"💵 Gain potentiel : {usd(a['shares'] - a['usd'])} $",
        f"🧩 {a['fills']} exécution(s)",
        "",
        f"🕐 Parié le {bet_time} ({tz_name})",
    ]
    if game_start:
        lines.append(f"🏟 Début du match : {fmt_time(game_start)} ({until(game_start, now, 'déjà commencé')})")
    # Pour le sport, la « fin » Polymarket n'est qu'une date limite (marge en cas de report) :
    # seule l'heure du match compte. Pour le reste, c'est l'échéance à laquelle le résultat est attendu.
    elif market_end:
        lines.append(f"⏳ Échéance du marché : {fmt_time(market_end)} ({until(market_end, now, 'dépassée')})")
    lines += [
        "",
        f"👤 <a href=\"{profile}\">{e(a['name'] or short)}</a> ({short})",
        f"🔗 <a href=\"{market}\">Voir le marché</a>",
    ]
    if a["tx"]:
        lines.append(f"⛓ <a href=\"https://polygonscan.com/tx/{a['tx']}\">Transaction</a>")
    return "\n".join(lines)


def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("---- (Telegram non configuré, affichage seulement) ----\n" + text + "\n")
        return
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram a répondu {resp.status}")


def resend(wallet, now):
    verdict, info = check_first_bet(wallet, now, force=True)
    if verdict != "alert":
        print(f"{wallet} : pas de premier pari >= {THRESHOLD_USD:.0f} $, rien à renvoyer.")
        return
    send_telegram(format_alert(info))
    print(f"Alerte renvoyée pour {wallet} : {info['usd']:.0f} $ sur « {info['title']} »")


def main():
    now = int(time.time())
    if RESEND_WALLET:
        return resend(RESEND_WALLET, now)
    state = load_state()

    trades = fetch_recent_trades(now - LOOKBACK_MIN * 60)
    candidates = find_candidates(trades) - state["old"].keys() - state["alerted"].keys()
    print(f"{len(trades)} achats >= {MIN_FILL_USD:.0f} $ sur {LOOKBACK_MIN} min, "
          f"{len(candidates)} compte(s) à vérifier")

    alerts = 0
    for wallet in sorted(candidates):
        try:
            verdict, info = check_first_bet(wallet, now)
        except Exception as e:
            print(f"  {wallet} : vérification impossible ({e})", file=sys.stderr)
            continue
        if verdict == "old":
            state["old"][wallet] = now
        elif verdict == "alert":
            send_telegram(format_alert(info))
            state["alerted"][wallet] = now
            alerts += 1
            print(f"  ALERTE {wallet} : {info['usd']:.0f} $ sur « {info['title']} »")
        time.sleep(0.25)

    save_state(state, now)
    print(f"Terminé : {alerts} alerte(s) envoyée(s).")


if __name__ == "__main__":
    main()
