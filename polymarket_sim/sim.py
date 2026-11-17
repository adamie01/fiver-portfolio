#!/usr/bin/env python3
"""Polymarket paper-trading simulator (fictional money only).

Reads only Polymarket's PUBLIC endpoints (Gamma for markets, CLOB for order
books). Never places orders, never authenticates, never asks for credentials.

Commands
  scan                      fetch up to 1000 active markets, apply filters, rank a review queue
  show ID                   print one candidate in full (rules, source, prices)
  estimate ID --p P --source S --reason R [--exit E]
                            record my probability (YES) for a market
  skip ID --reason R        mark a market as not tradable (e.g. no official source)
  evaluate [--execute]      size bets for markets with fresh estimates; with --execute, enter them
  review                    mark open bets to market, apply exit rules, check drawdown
  status                    bankroll, peak, open bets
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
STATE = os.path.join(ROOT, "state.json")
ESTIMATES = os.path.join(ROOT, "estimates.json")
CANDIDATES = os.path.join(ROOT, "candidates.json")
JOURNAL = os.path.join(REPO, "paris.txt")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

START_BANKROLL = 50.0
MIN_HOURS_LEFT = 2.0
MIN_LIQUIDITY = 5000.0
MIN_EDGE = 0.08            # 8 points
MAX_LIQ_LOSS = 0.03        # full position must be resellable losing <= 3%
KELLY_MULT = 0.5
MAX_STAKE_FRAC = 0.06
MAX_OPEN = 5
EXIT_PROB_MOVE = 0.05      # my probability moved > 5 points
EXIT_GAP_CLOSED = 0.02     # remaining edge vs executable exit price <= 2 points
EXIT_HOURS_LEFT = 1.0
MAX_DRAWDOWN = 0.40
ESTIMATE_TTL_H = 6.0


# ---------------------------------------------------------------- utilities
def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def get_json(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-sim/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def journal(text):
    with open(JOURNAL, "a") as f:
        f.write(text.rstrip() + "\n\n")


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_state():
    st = load(STATE, None)
    if st is None:
        st = {"cash": START_BANKROLL, "peak": START_BANKROLL, "equity": START_BANKROLL,
              "open": [], "closed": [], "stopped": False, "stop_reason": None,
              "started": iso(now())}
        save(STATE, st)
        journal("=" * 70 + f"\nDÉBUT DE LA SIMULATION — {iso(now())}\n"
                f"Cagnotte fictive : {START_BANKROLL:.2f} $. Aucun pari réel.\n" + "=" * 70)
    return st


# ---------------------------------------------------------------- fees/book
def fee_rate(m):
    fs = m.get("feeSchedule") or {}
    if not m.get("feesEnabled"):
        return 0.0
    try:
        return float(fs.get("rate", 0.0))
    except (TypeError, ValueError):
        return 0.0


def taker_fee(shares, price, rate):
    return shares * rate * price * (1 - price)


def book(token_id):
    b = get_json(f"{CLOB}/book?token_id={token_id}")
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    return bids, asks, float(b.get("min_order_size") or 0)


def buy_with_dollars(asks, dollars, rate):
    """Walk asks spending `dollars` (fees included). Returns shares, cost, avg."""
    shares = cost = 0.0
    for p, sz in asks:
        per_share = p + rate * p * (1 - p)
        can = min(sz, (dollars - cost) / per_share)
        if can <= 0:
            break
        shares += can
        cost += can * per_share
        if cost >= dollars - 1e-9:
            break
    return shares, cost, (cost / shares if shares else None)


def sell_shares(bids, shares, rate):
    """Walk bids selling `shares`. Returns proceeds net of fees, filled shares."""
    left, proceeds = shares, 0.0
    for p, sz in bids:
        q = min(sz, left)
        proceeds += q * p - taker_fee(q, p, rate)
        left -= q
        if left <= 1e-9:
            break
    return proceeds, shares - left


# ---------------------------------------------------------------- scan
def fetch_markets(limit_total=1000):
    out, offset = [], 0
    while len(out) < limit_total:
        q = urllib.parse.urlencode({"active": "true", "closed": "false", "limit": 100,
                                    "offset": offset, "order": "volume24hr", "ascending": "false"})
        page = get_json(f"{GAMMA}/markets?{q}")
        if not page:
            break
        out.extend(page)
        offset += len(page)
        if len(page) < 100:
            break
    return out[:limit_total]


SOURCE_HINTS = ("resolution source", "official", "according to", "as reported by", "will be",
                ".gov", ".com", ".org", "data", "announce", "results")


def cmd_scan(a):
    st = load_state()
    if st["stopped"]:
        print("SIMULATION ARRÊTÉE:", st["stop_reason"]); return
    raw = fetch_markets()
    t = now()
    kept, reasons = [], {"ends<2h": 0, "liq<5000": 0, "not_binary/no_book": 0}
    for m in raw:
        end = parse_dt(m.get("endDate"))
        if not end or end - t < timedelta(hours=MIN_HOURS_LEFT):
            reasons["ends<2h"] += 1; continue
        if float(m.get("liquidityNum") or 0) < MIN_LIQUIDITY:
            reasons["liq<5000"] += 1; continue
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except (ValueError, TypeError):
            outcomes, tokens, prices = [], [], []
        if len(tokens) != 2 or not m.get("enableOrderBook") or not m.get("acceptingOrders"):
            reasons["not_binary/no_book"] += 1; continue
        desc = m.get("description") or ""
        kept.append({
            "id": str(m["id"]), "question": m.get("question"), "slug": m.get("slug"),
            "event": (m.get("events") or [{}])[0].get("title"),
            "end": m.get("endDate"), "hours_left": round((end - t).total_seconds() / 3600, 1),
            "liquidity": round(float(m.get("liquidityNum") or 0)),
            "volume24h": round(float(m.get("volume24hr") or 0)),
            "outcomes": outcomes, "tokens": tokens, "prices": prices,
            "bestBid": m.get("bestBid"), "bestAsk": m.get("bestAsk"),
            "fee_rate": fee_rate(m), "resolutionSource": m.get("resolutionSource") or "",
            "description": desc,
            "source_hint": bool(m.get("resolutionSource")) or any(h in desc.lower() for h in SOURCE_HINTS),
        })
    est = load(ESTIMATES, {})
    held = {p["market_id"] for p in st["open"]}

    def prio(c):
        p = c["prices"][0] if c["prices"] else 0.5
        mid = 0.05 <= p <= 0.95
        fresh = c["id"] in est and age_h(est[c["id"]]) < ESTIMATE_TTL_H
        soon = c["hours_left"] <= 24 * 45
        return (c["id"] in held, not fresh and mid and soon and c["source_hint"],
                mid, soon, c["volume24h"])

    kept.sort(key=prio, reverse=True)
    save(CANDIDATES, {"scanned_at": iso(t), "fetched": len(raw), "kept": kept})
    print(f"[{iso(t)}] fetched={len(raw)} kept={len(kept)} rejected={reasons}")
    print("Review queue (held first, then unassessed mid-priced near-term):")
    for c in kept[:a.top]:
        tag = "HELD " if c["id"] in held else ("est  " if c["id"] in est else "     ")
        print(f" {tag}{c['id']:>8} yes={c['prices'][0] if c['prices'] else '?':<6} "
              f"{c['hours_left']:>7}h liq={c['liquidity']:>9} v24={c['volume24h']:>9} | {c['question'][:90]}")


def age_h(e):
    return (now() - parse_dt(e["at"])).total_seconds() / 3600


def find_candidate(mid):
    for c in load(CANDIDATES, {"kept": []})["kept"]:
        if c["id"] == str(mid):
            return c
    m = get_json(f"{GAMMA}/markets/{mid}")
    return {"id": str(m["id"]), "question": m.get("question"), "end": m.get("endDate"),
            "tokens": json.loads(m.get("clobTokenIds") or "[]"),
            "outcomes": json.loads(m.get("outcomes") or "[]"),
            "prices": [float(x) for x in json.loads(m.get("outcomePrices") or "[]")],
            "fee_rate": fee_rate(m), "description": m.get("description") or "",
            "resolutionSource": m.get("resolutionSource") or "", "slug": m.get("slug")}


def cmd_show(a):
    c = find_candidate(a.id)
    for k in ("id", "question", "event", "slug", "end", "hours_left", "liquidity", "volume24h",
              "outcomes", "prices", "bestBid", "bestAsk", "fee_rate", "resolutionSource"):
        print(f"{k}: {c.get(k)}")
    print("RULES:\n" + c.get("description", ""))


def cmd_estimate(a):
    est = load(ESTIMATES, {})
    prev = est.get(str(a.id))
    est[str(a.id)] = {"p": a.p, "source": a.source, "reason": a.reason, "exit": a.exit,
                      "at": now().isoformat(), "skip": False,
                      "history": (prev or {}).get("history", []) + [[now().isoformat(), a.p]]}
    save(ESTIMATES, est)
    print(f"estimate {a.id}: p(YES)={a.p:.3f}" + (f" (was {prev['p']:.3f})" if prev and not prev.get("skip") else ""))


def cmd_skip(a):
    est = load(ESTIMATES, {})
    est[str(a.id)] = {"skip": True, "reason": a.reason, "at": now().isoformat()}
    save(ESTIMATES, est)
    print(f"skip {a.id}: {a.reason}")


# ---------------------------------------------------------------- valuation
def position_value(pos):
    bids, _, _ = book(pos["token"])
    proceeds, filled = sell_shares(bids, pos["shares"], pos["fee_rate"])
    best_bid = bids[0][0] if bids else 0.0
    return proceeds, filled, best_bid


def mark_to_market(st):
    total = st["cash"]
    for p in st["open"]:
        try:
            v, _, bb = position_value(p)
            p["mark"], p["best_bid"] = round(v, 4), bb
        except RuntimeError:
            v = p.get("mark", p["cost"])
        total += v
    st["equity"] = round(total, 4)
    st["peak"] = max(st["peak"], st["equity"])
    return st["equity"]


def check_stop(st):
    if st["stopped"]:
        return True
    dd = 1 - st["equity"] / st["peak"] if st["peak"] > 0 else 1
    reason = None
    if st["equity"] <= 0.01:
        reason = "cagnotte fictive à 0"
    elif dd >= MAX_DRAWDOWN:
        reason = f"perte de {dd:.1%} depuis le plus haut ({st['peak']:.2f} $)"
    if not reason:
        return False
    # close everything at executable prices, then write the report
    for p in list(st["open"]):
        close_position(st, p, "arrêt général : " + reason)
    st["equity"] = round(st["cash"], 4)
    st["stopped"], st["stop_reason"] = True, reason
    write_report(st, reason)
    save(STATE, st)
    print("STOP:", reason)
    return True


def write_report(st, reason):
    closed = st["closed"]
    wins = [c for c in closed if c["pnl"] > 0]
    lines = ["=" * 70, f"BILAN — ARRÊT DE LA SIMULATION — {iso(now())}", f"Raison : {reason}",
             f"Cagnotte de départ : {START_BANKROLL:.2f} $ | plus haut : {st['peak']:.2f} $ | "
             f"finale : {st['equity']:.2f} $ ({st['equity'] / START_BANKROLL - 1:+.1%})",
             f"Paris clôturés : {len(closed)} | gagnants : {len(wins)} | perdants : {len(closed) - len(wins)}"]
    for c in closed:
        lines.append(f" - {c['question'][:70]} [{c['side']}] mise {c['cost']:.2f} → {c['proceeds']:.2f} "
                     f"({c['pnl']:+.2f} $) — {c['exit_reason']}")
    lines.append("=" * 70)
    journal("\n".join(lines))


# ---------------------------------------------------------------- evaluate/enter
def cmd_evaluate(a):
    st = load_state()
    if st["stopped"]:
        print("SIMULATION ARRÊTÉE:", st["stop_reason"]); return
    equity = mark_to_market(st)
    if check_stop(st):
        return
    est = load(ESTIMATES, {})
    held = {p["market_id"] for p in st["open"]}
    cands = {c["id"]: c for c in load(CANDIDATES, {"kept": []})["kept"]}
    proposals = []
    for mid, e in est.items():
        if e.get("skip") or mid in held or age_h(e) > ESTIMATE_TTL_H or mid not in cands:
            continue
        c = cands[mid]
        if c["hours_left"] < MIN_HOURS_LEFT:
            continue
        for side, idx, q in (("YES", 0, e["p"]), ("NO", 1, 1 - e["p"])):
            try:
                bids, asks, min_size = book(c["tokens"][idx])
            except RuntimeError as err:
                print("book error", mid, err); continue
            if not asks or not bids:
                continue
            ask = asks[0][0]
            edge = q - ask
            if edge <= MIN_EDGE:
                continue
            rate = c["fee_rate"]
            unit = ask + rate * ask * (1 - ask)
            kelly = max(0.0, (q - unit) / (1 - unit))
            stake = min(kelly * KELLY_MULT, MAX_STAKE_FRAC) * equity
            stake = min(stake, st["cash"])
            shares, cost, avg = buy_with_dollars(asks, stake, rate)
            note = []
            if shares < max(min_size, 1e-9):
                note.append(f"taille {shares:.2f} < minimum {min_size}")
            proceeds, filled = sell_shares(bids, shares, rate)
            loss = 1 - proceeds / cost if cost else 1
            if filled < shares - 1e-6:
                note.append("carnet trop mince pour tout revendre")
            if loss > MAX_LIQ_LOSS:
                note.append(f"revente immédiate -{loss:.1%} > 3%")
            if q - (avg or 1) <= MIN_EDGE:
                note.append("écart < 8 pts après slippage/frais")
            proposals.append({"mid": mid, "c": c, "e": e, "side": side, "idx": idx, "q": q, "ask": ask,
                              "edge": edge, "kelly": kelly, "stake": cost, "shares": shares,
                              "avg": avg, "liq_loss": loss, "ok": not note, "note": note})
    proposals.sort(key=lambda x: x["edge"], reverse=True)
    if not proposals:
        print(f"Aucun écart > 8 pts parmi les estimations fraîches. Équité {equity:.2f} $.")
    for p in proposals:
        flag = "OK " if p["ok"] else "NON"
        print(f"{flag} {p['mid']} {p['side']} p_moi={p['q']:.3f} ask={p['ask']:.3f} écart={p['edge'] * 100:.1f}pts "
              f"kelly={p['kelly']:.3f} mise={p['stake']:.2f}$ revente={-p['liq_loss']:+.1%} {'; '.join(p['note'])} "
              f"| {p['c']['question'][:70]}")
    if not a.execute:
        return
    for p in proposals:
        if not p["ok"]:
            continue
        if len(st["open"]) >= MAX_OPEN:
            print("5 paris ouverts : plus d'entrée."); break
        if p["mid"] in {o["market_id"] for o in st["open"]}:
            continue
        if p["stake"] > st["cash"] + 1e-9 or p["stake"] < 0.01:
            continue
        enter(st, p)
    save(STATE, st)


def enter(st, p):
    c, e = p["c"], p["e"]
    exit_rule = e.get("exit") or (f"sortie si l'écart se referme (≤ 2 pts), si ma proba bouge de > 5 pts, "
                                  f"ou si le marché finit dans l'heure (fin {c['end']})")
    # Step 8: write the journal entry BEFORE the position exists
    journal(f"[{iso(now())}] ENTRÉE (fictive)\n"
            f"  Marché     : {c['question']} (id {c['id']}, polymarket.com/market/{c.get('slug', '')})\n"
            f"  Côté       : {p['side']}\n"
            f"  Ma proba   : {p['q']:.1%} ({p['side']})  | proba OUI = {e['p']:.1%}\n"
            f"  Prix       : ask {p['ask']:.3f}, prix moyen payé {p['avg']:.4f} frais inclus ({p['shares']:.2f} parts)\n"
            f"  Mise       : {p['stake']:.2f} $ fictifs (Kelly {p['kelly']:.3f} → ½ → plafond 6 %)\n"
            f"  Revente    : test immédiat {-p['liq_loss']:+.2%} (limite -3 %)\n"
            f"  Source     : {e.get('source')}\n"
            f"  Sortie     : {exit_rule}\n"
            f"  Raison     : {e.get('reason')}")
    st["cash"] = round(st["cash"] - p["stake"], 6)
    st["open"].append({"market_id": p["mid"], "question": c["question"], "side": p["side"],
                       "token": c["tokens"][p["idx"]], "shares": p["shares"], "cost": p["stake"],
                       "avg": p["avg"], "entry_p_yes": e["p"], "q_side": p["q"], "fee_rate": c["fee_rate"],
                       "end": c["end"], "opened": iso(now()), "exit_rule": exit_rule})
    print(f"ENTRÉE {p['side']} {p['mid']} mise {p['stake']:.2f}$")


def close_position(st, pos, reason):
    try:
        proceeds, filled, bb = position_value(pos)
    except RuntimeError:
        proceeds, filled, bb = pos.get("mark", 0.0), pos["shares"], pos.get("best_bid", 0)
    pnl = proceeds - pos["cost"]
    st["cash"] = round(st["cash"] + proceeds, 6)
    st["open"].remove(pos)
    rec = dict(pos, proceeds=round(proceeds, 4), pnl=round(pnl, 4), exit_reason=reason, closed_at=iso(now()))
    st["closed"].append(rec)
    journal(f"[{iso(now())}] SORTIE (fictive)\n"
            f"  Marché     : {pos['question']} (id {pos['market_id']}) [{pos['side']}]\n"
            f"  Revente    : {filled:.2f} parts, meilleur bid {bb:.3f}, produit net {proceeds:.2f} $\n"
            f"  Résultat   : {pnl:+.2f} $ (mise {pos['cost']:.2f} $)\n"
            f"  Raison     : {reason}\n"
            f"  Cagnotte   : {st['cash']:.2f} $ en liquide")
    print(f"SORTIE {pos['market_id']} {pnl:+.2f}$ — {reason}")


def cmd_review(a):
    st = load_state()
    if st["stopped"]:
        print("SIMULATION ARRÊTÉE:", st["stop_reason"]); return
    est = load(ESTIMATES, {})
    for pos in list(st["open"]):
        end = parse_dt(pos["end"])
        hours = (end - now()).total_seconds() / 3600 if end else 999
        e = est.get(pos["market_id"], {})
        p_yes = e.get("p", pos["entry_p_yes"])
        q_now = p_yes if pos["side"] == "YES" else 1 - p_yes
        try:
            proceeds, filled, bb = position_value(pos)
        except RuntimeError as err:
            print("book error", pos["market_id"], err); continue
        reason = None
        if hours <= EXIT_HOURS_LEFT:
            reason = f"le marché se termine dans {hours:.1f} h"
        elif abs(p_yes - pos["entry_p_yes"]) > EXIT_PROB_MOVE:
            reason = (f"ma probabilité OUI a bougé de {pos['entry_p_yes']:.1%} à {p_yes:.1%} "
                      f"(> 5 pts)")
        elif q_now - bb <= EXIT_GAP_CLOSED:
            reason = f"écart refermé : prix de sortie {bb:.3f} vs ma proba {q_now:.3f}"
        if reason:
            close_position(st, pos, reason)
        else:
            print(f"GARDE {pos['market_id']} [{pos['side']}] bid={bb:.3f} moi={q_now:.3f} "
                  f"valeur={proceeds:.2f}$ (mise {pos['cost']:.2f}$) fin dans {hours:.1f}h")
    eq = mark_to_market(st)
    save(STATE, st)
    if not check_stop(st):
        dd = 1 - eq / st["peak"]
        print(f"Équité {eq:.2f}$ | liquide {st['cash']:.2f}$ | plus haut {st['peak']:.2f}$ | "
              f"drawdown {dd:.1%} | ouverts {len(st['open'])}/5")


def cmd_status(a):
    st = load_state()
    print(json.dumps({k: st[k] for k in ("cash", "equity", "peak", "stopped", "stop_reason")}, indent=1))
    for p in st["open"]:
        print(f" {p['market_id']} [{p['side']}] mise {p['cost']:.2f} mark {p.get('mark')} | {p['question'][:80]}")
    print(f"clôturés: {len(st['closed'])}, PnL réalisé: {sum(c['pnl'] for c in st['closed']):+.2f}$")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("scan"); s.add_argument("--top", type=int, default=25); s.set_defaults(f=cmd_scan)
    s = sp.add_parser("show"); s.add_argument("id"); s.set_defaults(f=cmd_show)
    s = sp.add_parser("estimate"); s.add_argument("id"); s.add_argument("--p", type=float, required=True)
    s.add_argument("--source", required=True); s.add_argument("--reason", required=True)
    s.add_argument("--exit", default=None); s.set_defaults(f=cmd_estimate)
    s = sp.add_parser("skip"); s.add_argument("id"); s.add_argument("--reason", required=True); s.set_defaults(f=cmd_skip)
    s = sp.add_parser("evaluate"); s.add_argument("--execute", action="store_true"); s.set_defaults(f=cmd_evaluate)
    sp.add_parser("review").set_defaults(f=cmd_review)
    sp.add_parser("status").set_defaults(f=cmd_status)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    sys.exit(main())
