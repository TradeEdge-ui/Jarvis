"""Trading-journal analytics and risk calculators. Pure Python, deterministic, no network.

Everything is split into OBSERVATIONS (computed facts), INTERPRETATIONS (hypotheses, with sample sizes
and a confidence label) and RECOMMENDATIONS. Small samples are flagged rather than over-read.
This is analysis of supplied data for education/review — not advice, and no outcome is predicted.
"""
from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MIN_SAMPLE = 30           # below this, patterns are reported as "insufficient data"
MIN_BUCKET = 8            # minimum trades in a bucket before comparing it

_ALIASES = {
    "date": ["date", "time", "open_time", "opened", "entry_time", "open time", "datetime", "timestamp", "close_time"],
    "symbol": ["symbol", "pair", "instrument", "ticker", "market"],
    "side": ["side", "direction", "type", "action"],
    "pnl": ["pnl", "profit", "p&l", "net_pnl", "net profit", "profit/loss", "result", "p/l"],
    "size": ["size", "lots", "lot", "volume", "quantity", "qty", "units"],
    "risk": ["risk", "risk_amount", "risk$", "risk_usd", "planned_risk"],
    "r": ["r", "r_multiple", "rr", "r-multiple"],
    "setup": ["setup", "strategy", "pattern", "tag"],
    "timeframe": ["timeframe", "tf", "time_frame"],
    "session": ["session"],
}


@dataclass
class Trade:
    when: datetime | None
    symbol: str
    side: str
    pnl: float
    size: float | None
    risk: float | None
    r: float | None
    setup: str
    timeframe: str
    session: str


def _num(v) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^\d.\-+eE()]", "", str(v).replace(",", ""))
    if s in ("", "-", "+", "."):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def _dt(v) -> datetime | None:
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y.%m.%d %H:%M:%S",
                "%Y.%m.%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _session_from_hour(h: int) -> str:
    """Approximate by UTC hour: Asia 00-07, London 07-12, New York/overlap 12-21, Off-hours otherwise."""
    if 0 <= h < 7:
        return "Asia"
    if 7 <= h < 12:
        return "London"
    if 12 <= h < 21:
        return "New York"
    return "Off-hours"


def load_trades(path: Path) -> tuple[list[Trade], list[str]]:
    warnings: list[str] = []
    rows: list[dict] = []
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        header = [str(h or "").strip() for h in next(it, [])]
        rows = [dict(zip(header, r)) for r in it]
    else:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    if not rows:
        return [], ["file has no data rows"]
    cols = {k.strip().lower(): k for k in rows[0].keys() if k}

    def pick(field: str) -> str | None:
        for a in _ALIASES[field]:
            if a in cols:
                return cols[a]
        return None
    m = {f: pick(f) for f in _ALIASES}
    if not m["pnl"] and not m["r"]:
        raise ValueError("no profit column found (expected one of: " + ", ".join(_ALIASES["pnl"] + _ALIASES["r"]) + ")")
    trades, skipped = [], 0
    for r in rows:
        pnl = _num(r.get(m["pnl"])) if m["pnl"] else None
        rr = _num(r.get(m["r"])) if m["r"] else None
        if pnl is None and rr is None:
            skipped += 1
            continue
        when = _dt(r.get(m["date"])) if m["date"] else None
        sess = (str(r.get(m["session"]) or "").strip() if m["session"] else "") or \
               (_session_from_hour(when.hour) if when and when.hour + when.minute + when.second > 0 else "")
        trades.append(Trade(when, str(r.get(m["symbol"]) or "").strip() if m["symbol"] else "",
                            str(r.get(m["side"]) or "").strip().lower() if m["side"] else "",
                            pnl if pnl is not None else rr, _num(r.get(m["size"])) if m["size"] else None,
                            _num(r.get(m["risk"])) if m["risk"] else None, rr,
                            str(r.get(m["setup"]) or "").strip() if m["setup"] else "",
                            str(r.get(m["timeframe"]) or "").strip() if m["timeframe"] else "", sess))
    if skipped:
        warnings.append(f"{skipped} row(s) skipped (no parsable profit value)")
    if not m["pnl"]:
        warnings.append("no money P&L column; statistics are in R-multiples")
    if any(t.when is None for t in trades):
        warnings.append("some trades have no parsable date; time-based breakdowns exclude them and ordering uses file order")
    if any(t.when for t in trades):
        trades.sort(key=lambda t: (t.when is None, t.when or datetime.min))
    return trades, warnings


def _stats(pnls: list[float]) -> dict:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp, gl = sum(wins), -sum(losses)
    equity, peak, mdd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "breakeven": n - len(wins) - len(losses),
        "win_rate": len(wins) / n if n else 0.0, "loss_rate": len(losses) / n if n else 0.0,
        "avg_win": gp / len(wins) if wins else 0.0, "avg_loss": -gl / len(losses) if losses else 0.0,
        "expectancy": sum(pnls) / n if n else 0.0, "total": sum(pnls),
        "profit_factor": (gp / gl) if gl > 0 else (math.inf if gp > 0 else 0.0),
        "max_drawdown": mdd, "best": max(pnls) if pnls else 0.0, "worst": min(pnls) if pnls else 0.0,
    }


def _group(trades: list[Trade], keyfn) -> dict[str, dict]:
    g: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        k = keyfn(t)
        if k:
            g[k].append(t.pnl)
    return {k: _stats(v) for k, v in sorted(g.items())}


def _streaks(pnls: list[float]) -> dict:
    best_w = best_l = cur_w = cur_l = 0
    for p in pnls:
        cur_w, cur_l = (cur_w + 1, 0) if p > 0 else (0, cur_l + 1) if p < 0 else (0, 0)
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    return {"max_win_streak": best_w, "max_loss_streak": best_l}


def analyze(trades: list[Trade], last: int | None = None, symbol: str | None = None) -> dict:
    if symbol:
        trades = [t for t in trades if t.symbol.lower() == symbol.lower()]
    if last:
        trades = trades[-last:]
    n = len(trades)
    out: dict = {"n": n, "observations": [], "interpretations": [], "recommendations": [], "tables": {}, "caveats": []}
    if n == 0:
        out["caveats"].append("No trades to analyse.")
        return out
    pnls = [t.pnl for t in trades]
    s = _stats(pnls)
    out["stats"] = {k: (None if isinstance(v, float) and math.isinf(v) else v) for k, v in s.items()}
    out["stats"]["profit_factor_infinite"] = math.isinf(s["profit_factor"])
    out["streaks"] = _streaks(pnls)
    pf = "∞ (no losing trades)" if math.isinf(s["profit_factor"]) else f"{s['profit_factor']:.2f}"
    obs = out["observations"]
    obs.append(f"{n} trades: {s['wins']} wins, {s['losses']} losses, {s['breakeven']} breakeven — win rate {s['win_rate']:.1%}.")
    obs.append(f"Average winner {s['avg_win']:.2f}, average loser {s['avg_loss']:.2f}; expectancy {s['expectancy']:.2f} per trade; "
               f"total {s['total']:.2f}; profit factor {pf}.")
    obs.append(f"Maximum drawdown (closed-trade equity curve) {s['max_drawdown']:.2f}; longest win streak "
               f"{out['streaks']['max_win_streak']}, longest loss streak {out['streaks']['max_loss_streak']}.")
    # breakdowns
    tz = {}
    if any(t.when for t in trades):
        tz["by_weekday"] = _group([t for t in trades if t.when], lambda t: t.when.strftime("%A"))
    if any(t.session for t in trades):
        tz["by_session"] = _group(trades, lambda t: t.session)
    if any(t.setup for t in trades):
        tz["by_setup"] = _group(trades, lambda t: t.setup)
    if any(t.timeframe for t in trades):
        tz["by_timeframe"] = _group(trades, lambda t: t.timeframe)
    out["tables"] = {k: {kk: {a: (None if isinstance(b, float) and math.isinf(b) else b) for a, b in vv.items()}
                         for kk, vv in v.items()} for k, v in tz.items()}

    if n < MIN_SAMPLE:
        out["caveats"].append(f"Only {n} trades (< {MIN_SAMPLE}): every statistic here is noisy; treat patterns as questions, not conclusions.")
    for name, table in tz.items():
        usable = {k: v for k, v in table.items() if v["trades"] >= MIN_BUCKET}
        if len(usable) >= 2:
            best = max(usable.items(), key=lambda kv: kv[1]["expectancy"])
            worst = min(usable.items(), key=lambda kv: kv[1]["expectancy"])
            if best[0] != worst[0]:
                label = name.replace("by_", "")
                obs.append(f"By {label}: best '{best[0]}' (expectancy {best[1]['expectancy']:.2f}, {best[1]['trades']} trades), "
                           f"worst '{worst[0]}' ({worst[1]['expectancy']:.2f}, {worst[1]['trades']} trades).")
                conf = "low" if n < MIN_SAMPLE or min(best[1]['trades'], worst[1]['trades']) < 15 else "moderate"
                out["interpretations"].append(
                    f"[{conf} confidence] The gap between '{best[0]}' and '{worst[0]}' ({label}) may reflect a real edge difference, "
                    f"or just variance in {best[1]['trades']}/{worst[1]['trades']} trades.")
        small = [k for k, v in table.items() if v["trades"] < MIN_BUCKET]
        if small:
            out["caveats"].append(f"{name}: too few trades to compare for {', '.join(small)} (< {MIN_BUCKET}).")

    # behavioural patterns
    _behaviour(trades, s, out)

    # recommendations
    rec = out["recommendations"]
    if s["expectancy"] < 0 and n >= MIN_SAMPLE:
        rec.append("Expectancy is negative over a meaningful sample: review the strategy rules before adding size.")
    if s["avg_loss"] and abs(s["avg_loss"]) > s["avg_win"] and s["win_rate"] < 0.6:
        rec.append("Average loser exceeds average winner while win rate is modest: check stop placement and whether winners are cut early.")
    if not rec:
        rec.append("Keep journaling consistently; re-run this analysis as the sample grows past "
                   f"{MIN_SAMPLE} trades per segment before changing the plan.")
    out["disclaimer"] = "Analysis of the supplied data for review and education only; not financial advice. Past results do not predict future results."
    return out


def _behaviour(trades: list[Trade], s: dict, out: dict) -> None:
    n = len(trades)
    pnls = [t.pnl for t in trades]
    obs, interp, rec = out["observations"], out["interpretations"], out["recommendations"]
    # losses after consecutive wins
    after_2w = [pnls[i] for i in range(2, n) if pnls[i - 1] > 0 and pnls[i - 2] > 0]
    other = [pnls[i] for i in range(2, n) if not (pnls[i - 1] > 0 and pnls[i - 2] > 0)]
    if len(after_2w) >= MIN_BUCKET and len(other) >= MIN_BUCKET:
        e1, e2 = sum(after_2w) / len(after_2w), sum(other) / len(other)
        l1 = sum(1 for p in after_2w if p < 0) / len(after_2w)
        l2 = sum(1 for p in other if p < 0) / len(other)
        obs.append(f"After two consecutive wins ({len(after_2w)} trades) the next trade averaged {e1:.2f} with {l1:.0%} losers, "
                   f"versus {e2:.2f} and {l2:.0%} losers otherwise ({len(other)} trades).")
        if e1 < e2 and l1 > l2 + 0.15:
            conf = "low" if len(after_2w) < 15 else "moderate"
            interp.append(f"[{conf} confidence] Trades following two wins do noticeably worse. One possible explanation is "
                          "overconfidence or looser selection after winning; variance alone can also produce this.")
            rec.append("Review the trades that follow two consecutive wins: was risk size or setup quality different from your plan?")
    # risk escalation after wins / loss
    sized = [(i, t) for i, t in enumerate(trades) if t.size]
    if len(sized) >= MIN_BUCKET * 2:
        base = sorted(t.size for _, t in sized)[len(sized) // 2]
        bump_after_win = [t for i, t in sized if i > 0 and trades[i - 1].pnl > 0 and t.size > 1.5 * base]
        if bump_after_win:
            share = sum(1 for t in bump_after_win if t.pnl < 0) / len(bump_after_win)
            obs.append(f"{len(bump_after_win)} trade(s) opened right after a win used > 1.5× your median size ({base:g}); {share:.0%} of them lost.")
            if len(bump_after_win) >= 5 and share >= 0.6:
                interp.append("[low confidence] Size increases after winners are associated with losses; this is a possible risk-consistency problem.")
                rec.append("Consider a fixed risk-per-trade rule that does not scale with recent results.")
        # revenge pattern: bigger size within 30 min after a loss
        rev = [t for i, t in sized if i > 0 and trades[i - 1].pnl < 0 and t.when and trades[i - 1].when
               and 0 <= (t.when - trades[i - 1].when).total_seconds() <= 1800 and t.size > 1.3 * base]
        if len(rev) >= 3:
            obs.append(f"{len(rev)} trade(s) were opened within 30 minutes of a loss at > 1.3× median size.")
            interp.append("[low confidence] This resembles 'revenge trading' (re-entering quickly with larger size after a loss).")
            rec.append("Add a cooling-off rule after a loss (e.g. a fixed pause before the next entry).")
    # risk consistency
    risks = [t.risk for t in trades if t.risk]
    if len(risks) >= MIN_BUCKET:
        mean = sum(risks) / len(risks)
        sd = math.sqrt(sum((x - mean) ** 2 for x in risks) / len(risks))
        cv = sd / mean if mean else 0
        obs.append(f"Planned risk per trade averaged {mean:.2f} with variation (CV) {cv:.0%}.")
        if cv > 0.5:
            interp.append("Risk per trade varies a lot (CV > 50%), which makes results harder to evaluate.")
            rec.append("Standardise risk per trade so performance reflects the strategy rather than sizing swings.")


def risk_reward(entry: float, stop: float, target: float, side: str = "long") -> dict:
    side = side.lower()
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    risk = (entry - stop) if side == "long" else (stop - entry)
    reward = (target - entry) if side == "long" else (entry - target)
    if risk <= 0:
        raise ValueError("stop must be on the losing side of entry")
    if reward <= 0:
        raise ValueError("target must be on the winning side of entry")
    rr = reward / risk
    return {"risk_per_unit": risk, "reward_per_unit": reward, "reward_to_risk": rr,
            "breakeven_win_rate": risk / (risk + reward)}


def position_size(balance: float, risk_percent: float, entry: float, stop: float, value_per_point: float = 1.0) -> dict:
    if balance <= 0 or not 0 < risk_percent <= 100:
        raise ValueError("balance must be > 0 and risk_percent in (0, 100]")
    dist = abs(entry - stop)
    if dist == 0:
        raise ValueError("entry and stop are the same price")
    risk_amount = balance * risk_percent / 100
    units = risk_amount / (dist * value_per_point)
    return {"risk_amount": risk_amount, "stop_distance": dist, "position_size": units}
