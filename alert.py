#!/usr/bin/env python3
"""
Statistiques Polymarket : tous les achats >= 1 000 $, et leur taux de réussite selon le prix d'entrée.

À chaque exécution (toutes les 5 min) :
  1. Récupère les achats >= MIN_BET_USD depuis l'exécution précédente et les ajoute aux paris en cours,
     regroupés par marché, issue et tranche de prix (le fichier reste petit même avec des milliers de paris).
  2. Une fois par jour, à partir de REPORT_HOUR (20 h, heure de Paris) : vérifie quels marchés en cours
     sont tranchés, les passe en gagnés / perdus / remboursés une fois pour toutes, et envoie le rapport
     sur Telegram (ou l'affiche dans la console si Telegram n'est pas configuré).

Tout est conservé dans stats.json. Uniquement la bibliothèque standard Python.
Usage ponctuel : python alert.py --backfill 7   (pré-remplit avec les 7 derniers jours)
                 python alert.py --report       (envoie le rapport tout de suite)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Paris"))
except Exception:
    TZ = timezone.utc

API = "https://data-api.polymarket.com/v2"
GAMMA_API = "https://gamma-api.polymarket.com"

MIN_BET_USD = float(os.getenv("MIN_BET_USD", "1000"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "20"))
MAX_LOOKBACK_H = int(os.getenv("MAX_LOOKBACK_H", "24"))
STATS_FILE = os.getenv("STATS_FILE", "stats.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
FORCE_REPORT = os.getenv("FORCE_REPORT", "").strip().lower() in ("1", "true", "yes")

# Tranches du prix payé (= probabilité donnée par le marché au moment du pari).
PRICE_BUCKETS = [(0, 25), (25, 50), (50, 75), (75, 90), (90, 95), (95, 99), (99, 101)]
BUCKET_LABELS = ["+99 %" if hi == 101 else f"{lo}-{hi} %" for lo, hi in PRICE_BUCKETS]
OUTCOMES = ("won", "lost", "void")


# ---------- API ----------

def get_json(base, path, params, retries=4):
    url = f"{base}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymarket-stats/2.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt * (5 if getattr(e, "code", None) == 429 else 1)
            print(f"  erreur API ({e}), nouvel essai dans {wait}s", file=sys.stderr)
            time.sleep(wait)


def fetch_buys(since_ts):
    """Achats (côté preneur) >= MIN_BET_USD depuis since_ts, du plus récent au plus ancien."""
    buys, cursor = [], None
    while True:
        params = {"limit": 1000, "takerOnly": "true", "filterType": "CASH", "filterAmount": int(MIN_BET_USD)}
        if cursor:
            params["cursor"] = cursor
        resp = get_json(API, "/trades", params)
        if "data" not in resp:
            raise RuntimeError(f"réponse inattendue de l'API : {resp}")
        batch = resp["data"]
        buys.extend(t for t in batch if t["timestamp"] >= since_ts and t["side"] == "BUY")
        pagination = resp.get("pagination") or {}
        cursor = pagination.get("next_cursor") if pagination.get("has_more") else None
        if not batch or not cursor or batch[-1]["timestamp"] < since_ts:
            return buys


def resolved_markets(condition_ids):
    """{condition_id: [prix final de chaque issue]} pour les marchés tranchés parmi condition_ids."""
    result = {}
    ids = list(condition_ids)
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        markets = get_json(GAMMA_API, "/markets", {"condition_ids": chunk, "closed": "true", "limit": 100})
        for m in markets or []:
            try:
                prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
            except (ValueError, TypeError):
                continue
            # Un marché clôturé mais pas encore tranché garde des prix intermédiaires : on attend.
            if m.get("umaResolutionStatus") != "resolved" and not all(p in (0.0, 0.5, 1.0) for p in prices):
                continue
            result[(m.get("conditionId") or "").lower()] = prices
        time.sleep(0.2)
    return result


# ---------- Statistiques (stats.json) ----------

def empty_results():
    return [{k: [0, 0.0, 0.0] for k in OUTCOMES} for _ in PRICE_BUCKETS]  # [nombre, gain/perte, misé]


def load_stats():
    try:
        with open(STATS_FILE) as f:
            stats = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        stats = {}
    stats.setdefault("started", int(time.time()))
    stats.setdefault("open", {})            # condition_id -> {"t": titre, "p": {issue: {tranche: [n, $, parts]}}}
    stats.setdefault("results", empty_results())
    stats.setdefault("tracked", [0, 0.0])   # [nombre d'achats, montant total]
    stats.setdefault("seen", {})            # identifiants des achats récents, pour ne jamais compter deux fois
    stats.setdefault("last_report", {})
    return stats


def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, separators=(",", ":"), ensure_ascii=False)


def bucket_of(price):
    pct = price * 100
    return next(i for i, (_, hi) in enumerate(PRICE_BUCKETS) if pct < hi or hi == 101)


def add_buys(stats, buys):
    added = 0
    for t in buys:
        key = f"{t['transaction_hash']}:{t['token_id']}"
        if key in stats["seen"]:
            continue
        stats["seen"][key] = t["timestamp"]
        usd_size, shares = t["size"] * t["price"], t["size"]
        market = stats["open"].setdefault(t["condition_id"].lower(), {"t": t.get("title", ""), "p": {}})
        cell = market["p"].setdefault(str(t["outcome_index"]), {}).setdefault(str(bucket_of(t["price"])), [0, 0.0, 0.0])
        cell[0] += 1
        cell[1] += usd_size
        cell[2] += shares
        stats["tracked"][0] += 1
        stats["tracked"][1] += usd_size
        added += 1
    return added


def resolve(stats):
    """Passe les marchés tranchés en résultats définitifs : ils ne sont plus jamais réanalysés."""
    finals = resolved_markets(stats["open"].keys())
    for cid, prices in finals.items():
        market = stats["open"].pop(cid, None)
        if not market:
            continue
        for idx, cells in market["p"].items():
            payout = prices[int(idx)] if int(idx) < len(prices) else 0.0
            kind = "won" if payout >= 0.99 else "lost" if payout <= 0.01 else "void"
            for b, (n, usd_size, shares) in cells.items():
                r = stats["results"][int(b)][kind]
                r[0] += n
                r[1] += shares * payout - usd_size
                r[2] += usd_size
    return len(finals)


# ---------- Rapport ----------

def usd(n):
    return f"{n:,.0f}".replace(",", " ")


def signed_usd(n):
    return f"{'+' if n >= 0 else '−'}{usd(abs(n))} $"


def short_usd(n):
    sign = "+" if n >= 0 else "-"
    n = abs(n)
    if n >= 1e6:
        return f"{sign}{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{sign}{n / 1e3:.0f}k"
    return f"{sign}{n:.0f}"


def totals(results):
    return {k: [sum(r[k][i] for r in results) for i in range(3)] for k in OUTCOMES}


def format_report(stats, resolved_now):
    res, prev = stats["results"], stats["last_report"]
    tot = totals(res)
    open_n = sum(c[0] for m in stats["open"].values() for p in m["p"].values() for c in p.values())
    open_usd = sum(c[1] for m in stats["open"].values() for p in m["p"].values() for c in p.values())
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    since = datetime.fromtimestamp(stats["started"], TZ).strftime("%d/%m")

    lines = [
        f"📊 <b>Rapport Polymarket du {today}</b>",
        f"Tous les achats ≥ {usd(MIN_BET_USD)} $, suivis depuis le {since}",
        "",
        f"🧾 Suivis : <b>{usd(stats['tracked'][0])}</b> achats ({usd(stats['tracked'][1])} $)",
    ]
    if prev:
        new = stats["tracked"][0] - prev.get("tracked", 0)
        done = sum(tot[k][0] for k in OUTCOMES) - prev.get("decided", 0)
        lines.append(f"🆕 Depuis hier : +{usd(new)} achats, {usd(done)} tranchés")
    lines += [
        f"✅ Gagnés : {usd(tot['won'][0])} ({signed_usd(tot['won'][1])})",
        f"❌ Perdus : {usd(tot['lost'][0])} ({signed_usd(tot['lost'][1])})",
    ]
    if tot["void"][0]:
        lines.append(f"⚖️ Remboursés : {usd(tot['void'][0])} ({signed_usd(tot['void'][1])})")
    lines.append(f"⏳ En cours : {usd(open_n)} ({usd(open_usd)} $ misés)")
    decided = tot["won"][0] + tot["lost"][0]
    if decided:
        pnl = sum(tot[k][1] for k in OUTCOMES)
        staked = sum(tot[k][2] for k in OUTCOMES)
        lines.append(f"Σ <b>{signed_usd(pnl)}</b> · réussite {tot['won'][0] / decided * 100:.0f} % · "
                     f"rendement {pnl / staked * 100:+.1f} %")

    rows = ["Prix     Gagnés Perdus Taux  Gain"]
    for label, r in zip(BUCKET_LABELS, res):
        w, l = r["won"][0], r["lost"][0]
        rate = f"{w / (w + l) * 100:.0f}%" if w + l else "–"
        pnl = short_usd(sum(r[k][1] for k in OUTCOMES)) if w + l else "–"
        rows.append(f"{label:<8} {w:>6} {l:>6} {rate:>4} {pnl:>6}")
    lines += ["", "🎯 <b>Réussite selon le prix d'entrée</b>", "<pre>" + "\n".join(rows) + "</pre>"]
    lines.append(f"<i>Gain = gain ou perte des parieurs, en supposant qu'ils gardent leur pari jusqu'au résultat. "
                 f"{usd(resolved_now)} marché(s) tranché(s) depuis le dernier rapport.</i>")
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


def report_due(stats, force):
    now = datetime.now(TZ)
    return force or (now.hour >= REPORT_HOUR and stats["last_report"].get("day") != now.strftime("%Y-%m-%d"))


def send_report(stats):
    resolved_now = resolve(stats)
    send_telegram(format_report(stats, resolved_now))
    tot = totals(stats["results"])
    stats["last_report"] = {
        "day": datetime.now(TZ).strftime("%Y-%m-%d"),
        "tracked": stats["tracked"][0],
        "decided": sum(tot[k][0] for k in OUTCOMES),
    }
    # Signale au workflow qu'il faut enregistrer stats.json dans le dépôt (sauvegarde quotidienne).
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("reported=true\n")
    print(f"Rapport envoyé ({resolved_now} marché(s) tranché(s)).")


def main():
    now = int(time.time())
    args = sys.argv[1:]
    stats = load_stats()

    if args[:1] == ["--backfill"]:
        days = float(args[1]) if len(args) > 1 else 7
        since = now - int(days * 86400)
        stats["started"] = min(stats["started"], since)
    else:
        since = max(stats.get("last_run", now - 3600) - 15 * 60, now - MAX_LOOKBACK_H * 3600)

    buys = fetch_buys(since)
    added = add_buys(stats, buys)
    stats["seen"] = {k: ts for k, ts in stats["seen"].items() if ts >= since - 3600}
    stats["last_run"] = now
    print(f"{added} nouvel(s) achat(s) >= {usd(MIN_BET_USD)} $ sur {(now - since) // 60} min "
          f"({len(stats['open'])} marché(s) en cours)")

    if report_due(stats, FORCE_REPORT or "--report" in args):
        send_report(stats)
    save_stats(stats)


if __name__ == "__main__":
    main()
