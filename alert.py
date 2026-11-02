#!/usr/bin/env python3
"""
Alerte Polymarket : nouveau compte dont le PREMIER pari dépasse un seuil (10 000 $ par défaut).

Fonctionnement à chaque exécution :
  1. Récupère les achats >= MIN_FILL_USD depuis l'exécution précédente (au moins 60 min, au plus 24 h).
  2. Additionne les morceaux par (compte, marché) : un gros ordre est souvent exécuté en plusieurs fois.
  3. Pour chaque candidat, lit l'historique complet du compte (du plus ancien au plus récent) et vérifie
     que son tout premier trade est sur ce marché, récent, et que ses achats sur ce marché pendant
     FIRST_BET_WINDOW_MIN minutes dépassent THRESHOLD_USD.
  4. Envoie une alerte Telegram (ou l'affiche dans la console si Telegram n'est pas configuré),
     suivie du bilan des paris déjà signalés (gagnés / perdus / en cours), conservé dans bets.json.

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

API = "https://data-api.polymarket.com/v2"
GAMMA_API = "https://gamma-api.polymarket.com"

THRESHOLD_USD = float(os.getenv("THRESHOLD_USD", "10000"))
MIN_FILL_USD = float(os.getenv("MIN_FILL_USD", "1000"))
MIN_LOOKBACK_MIN = int(os.getenv("LOOKBACK_MIN", "60"))
MAX_LOOKBACK_H = int(os.getenv("MAX_LOOKBACK_H", "24"))
FIRST_BET_WINDOW_MIN = int(os.getenv("FIRST_BET_WINDOW_MIN", "30"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
BETS_FILE = os.getenv("BETS_FILE", "bets.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# Renvoi manuel : renvoie l'alerte de ce compte, même ancienne ou déjà envoyée.
RESEND_WALLET = os.getenv("RESEND_WALLET", "").strip().lower()

# Comptes déjà identifiés comme anciens : on les garde 30 jours pour éviter de les revérifier.
OLD_WALLET_TTL = 30 * 24 * 3600
ALERTED_TTL = 7 * 24 * 3600


def get_json(path, params, retries=3, base=API):
    url = f"{base}{path}?{urllib.parse.urlencode(params, doseq=True)}"
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


def data_api(path, params):
    """Data API v2 : renvoie (lignes, curseur de la page suivante ou None)."""
    resp = get_json(path, params)
    if "data" not in resp:
        raise RuntimeError(f"réponse inattendue de l'API : {resp}")
    pagination = resp.get("pagination") or {}
    return resp["data"], pagination.get("next_cursor") if pagination.get("has_more") else None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_state():
    state = load_json(STATE_FILE, {})
    state.setdefault("old", {})
    state.setdefault("alerted", {})
    return state


def save_state(state, now):
    state["old"] = {w: t for w, t in state["old"].items() if now - t < OLD_WALLET_TTL}
    state["alerted"] = {w: t for w, t in state["alerted"].items() if now - t < ALERTED_TTL}
    state["last_run"] = now
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def lookback_start(state, now):
    """Depuis l'exécution précédente (GitHub espace parfois ses lancements de plusieurs heures)."""
    since = state.get("last_run", now) - 15 * 60
    return max(min(since, now - MIN_LOOKBACK_MIN * 60), now - MAX_LOOKBACK_H * 3600)


def fetch_recent_trades(since_ts):
    """Achats >= MIN_FILL_USD depuis since_ts, page par page (du plus récent au plus ancien)."""
    trades, cursor = [], None
    for _ in range(100):
        params = {"limit": 1000, "takerOnly": "true", "filterType": "CASH", "filterAmount": int(MIN_FILL_USD)}
        if cursor:
            params["cursor"] = cursor
        batch, cursor = data_api("/trades", params)
        trades.extend(t for t in batch if t["timestamp"] >= since_ts)
        if not batch or not cursor or batch[-1]["timestamp"] < since_ts:
            break
    return trades


def find_candidates(trades):
    """(compte, marché) dont les achats cumulés approchent le seuil."""
    totals = defaultdict(float)
    for t in trades:
        if t["side"] == "BUY":
            totals[(t["proxy_wallet"], t["condition_id"])] += t["size"] * t["price"]
    # Marge de 20 % : les morceaux < MIN_FILL_USD ne sont pas vus ici, le vrai total est recalculé ensuite.
    return {w for (w, _), usd in totals.items() if usd >= THRESHOLD_USD * 0.8}


def check_first_bet(wallet, now, max_age=None):
    """
    Renvoie ("alert", infos), ("old", None) si le compte a déjà un historique,
    ou ("pending", None) si c'est trop tôt pour conclure. max_age=None : pas de limite d'ancienneté.
    """
    activity, _ = data_api("/activity", {
        "user": wallet, "type": "TRADE", "limit": 200,
        "sortBy": "TIMESTAMP", "sortDirection": "ASC",
    })
    if not activity:
        return "pending", None

    first = activity[0]
    if max_age is not None and now - first["timestamp"] > max_age:
        return "old", None

    window_end = first["timestamp"] + FIRST_BET_WINDOW_MIN * 60
    market = first["condition_id"]
    buys = [a for a in activity
            if a["condition_id"] == market and a["side"] == "BUY" and a["timestamp"] <= window_end]
    total_usd = sum(float(a.get("usdc_size") or 0) for a in buys)

    if total_usd >= THRESHOLD_USD:
        by_outcome = defaultdict(float)
        for a in buys:
            by_outcome[a.get("outcome_index", 0)] += float(a.get("usdc_size") or 0)
        index = max(by_outcome, key=by_outcome.get)
        main_buys = [a for a in buys if a.get("outcome_index", 0) == index]
        bet_usd = sum(float(a.get("usdc_size") or 0) for a in main_buys)
        shares = sum(float(a.get("size") or 0) for a in main_buys)
        return "alert", {
            "wallet": wallet,
            "name": first.get("name") or first.get("pseudonym") or "",
            "title": first.get("title", ""),
            "event_slug": first.get("event_slug") or first.get("slug") or "",
            "outcome": main_buys[0].get("outcome", "?"),
            "outcome_index": index,
            "usd": bet_usd,
            "shares": shares,
            "avg_price": bet_usd / shares if shares else 0,
            "fills": len(buys),
            "condition_id": market,
            "first_ts": first["timestamp"],
            "last_ts": buys[-1]["timestamp"],
            "tx": first.get("transaction_hash", ""),
        }
    if now > window_end and max_age is not None:
        return "old", None
    return "pending", None


def gamma_market(condition_id):
    """Fiche Gamma d'un marché, ouvert ou clôturé (Gamma ne renvoie les clôturés qu'avec closed=true)."""
    for extra in ({}, {"closed": "true"}):
        markets = get_json("/markets", {"condition_ids": condition_id, **extra}, base=GAMMA_API)
        for m in markets or []:
            if (m.get("conditionId") or "").lower() == condition_id.lower():
                return m
    return None


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
        m = gamma_market(condition_id)
    except Exception as e:
        print(f"  dates du marché indisponibles ({e})", file=sys.stderr)
        return None, None
    if not m:
        return None, None
    return parse_date(m.get("gameStartTime")), parse_date(m.get("endDate"))


# ---------- Suivi des paris signalés (bets.json, enregistré dans le dépôt) ----------

def load_bets():
    return load_json(BETS_FILE, [])


def save_bets(bets):
    with open(BETS_FILE, "w") as f:
        json.dump(bets, f, indent=1, ensure_ascii=False)
        f.write("\n")


def record_bet(bets, info):
    if any(b["wallet"] == info["wallet"] for b in bets):
        return
    bets.append({
        "wallet": info["wallet"], "condition_id": info["condition_id"],
        "outcome_index": info["outcome_index"], "outcome": info["outcome"], "title": info["title"],
        "usd": round(info["usd"], 2), "shares": round(info["shares"], 4),
        "bet_ts": info["first_ts"], "status": "open", "pnl": None,
    })


def resolve_open_bets(bets):
    """Vérifie uniquement les paris encore en cours ; un pari clôturé n'est plus jamais réanalysé."""
    changed = False
    for b in bets:
        if b["status"] != "open":
            continue
        try:
            m = gamma_market(b["condition_id"])
        except Exception as e:
            print(f"  résultat indisponible pour « {b['title']} » ({e})", file=sys.stderr)
            continue
        if not m or not m.get("closed"):
            continue
        try:
            prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
            payout = prices[b["outcome_index"]]
        except (ValueError, IndexError, TypeError):
            continue
        # Un marché clôturé mais pas encore tranché garde des prix intermédiaires : on attend.
        if m.get("umaResolutionStatus") != "resolved" and payout not in (0.0, 0.5, 1.0):
            continue
        b["pnl"] = round(b["shares"] * payout - b["usd"], 2)
        b["status"] = "won" if payout >= 0.99 else "lost" if payout <= 0.01 else "void"
        b["resolved_ts"] = int(time.time())
        changed = True
        time.sleep(0.1)
    return changed


def signed_usd(n):
    return f"{'+' if n >= 0 else '−'}{usd(abs(n))} $"


def format_summary(bets):
    won = [b for b in bets if b["status"] == "won"]
    lost = [b for b in bets if b["status"] == "lost"]
    void = [b for b in bets if b["status"] == "void"]
    open_ = [b for b in bets if b["status"] == "open"]
    won_usd = sum(b["pnl"] for b in won)
    lost_usd = sum(b["pnl"] for b in lost)
    lines = [
        "",
        f"📈 <b>Bilan des {len(bets)} pari(s) signalé(s)</b>",
        f"✅ Gagnés : {len(won)} ({signed_usd(won_usd)})",
        f"❌ Perdus : {len(lost)} ({signed_usd(lost_usd)})",
    ]
    if void:
        lines.append(f"⚖️ Annulés / remboursés : {len(void)} ({signed_usd(sum(b['pnl'] for b in void))})")
    lines.append(f"⏳ En cours : {len(open_)} ({usd(sum(b['usd'] for b in open_))} $ misés)")
    if won or lost:
        total = sum(b["pnl"] for b in won + lost + void)
        rate = len(won) / (len(won) + len(lost)) * 100
        lines.append(f"Σ <b>{signed_usd(total)}</b> · réussite {rate:.0f} %")
    return "\n".join(lines)


# ---------- Message ----------

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


class Notifier:
    """Envoie les alertes ; met le bilan à jour une seule fois par exécution, au premier message."""

    def __init__(self, bets):
        self.bets = bets
        self.refreshed = False

    def send(self, info):
        record_bet(self.bets, info)
        if not self.refreshed:
            resolve_open_bets(self.bets)
            self.refreshed = True
        send_telegram(format_alert(info) + "\n" + format_summary(self.bets))


def seed_bets(bets, wallets, now):
    """Ajoute au suivi les comptes déjà signalés avant l'existence du bilan."""
    known = {b["wallet"] for b in bets}
    for wallet in sorted(set(wallets) - known):
        try:
            verdict, info = check_first_bet(wallet, now)
        except Exception as e:
            print(f"  {wallet} : reprise impossible ({e})", file=sys.stderr)
            continue
        if verdict == "alert":
            record_bet(bets, info)
            print(f"  repris dans le bilan : {wallet} ({info['usd']:.0f} $ sur « {info['title']} »)")
        time.sleep(0.25)


def resend(wallet, now):
    verdict, info = check_first_bet(wallet, now)
    if verdict != "alert":
        print(f"{wallet} : pas de premier pari >= {THRESHOLD_USD:.0f} $, rien à renvoyer.")
        return
    bets = load_bets()
    Notifier(bets).send(info)
    save_bets(bets)
    print(f"Alerte renvoyée pour {wallet} : {info['usd']:.0f} $ sur « {info['title']} »")


def main():
    now = int(time.time())
    if RESEND_WALLET:
        return resend(RESEND_WALLET, now)
    state = load_state()
    bets = load_bets()
    seed_bets(bets, state["alerted"].keys(), now)

    since = lookback_start(state, now)
    trades = fetch_recent_trades(since)
    known = state["old"].keys() | state["alerted"].keys() | {b["wallet"] for b in bets}
    candidates = find_candidates(trades) - known
    print(f"{len(trades)} achats >= {MIN_FILL_USD:.0f} $ sur {(now - since) // 60} min, "
          f"{len(candidates)} compte(s) à vérifier")

    notifier = Notifier(bets)
    alerts = 0
    for wallet in sorted(candidates):
        try:
            verdict, info = check_first_bet(wallet, now, max_age=now - since + 15 * 60)
        except Exception as e:
            print(f"  {wallet} : vérification impossible ({e})", file=sys.stderr)
            continue
        if verdict == "old":
            state["old"][wallet] = now
        elif verdict == "alert":
            notifier.send(info)
            state["alerted"][wallet] = now
            alerts += 1
            print(f"  ALERTE {wallet} : {info['usd']:.0f} $ sur « {info['title']} »")
        time.sleep(0.25)

    save_bets(bets)
    save_state(state, now)
    print(f"Terminé : {alerts} alerte(s) envoyée(s).")


if __name__ == "__main__":
    main()
