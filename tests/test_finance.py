import math
from datetime import datetime, timedelta

import pytest

from friday import finance as fin
from friday.finance import Trade
from conftest import run


def T(pnl, when=None, size=None, risk=None, setup="", session="", tf="", symbol="XAUUSD"):
    return Trade(when, symbol, "buy", pnl, size, risk, None, setup, tf, session)


def test_core_statistics_against_hand_calculation():
    s = fin._stats([100, -50, 200, -100, 50])
    assert s["trades"] == 5 and s["wins"] == 3 and s["losses"] == 2
    assert s["win_rate"] == pytest.approx(0.6) and s["loss_rate"] == pytest.approx(0.4)
    assert s["avg_win"] == pytest.approx(350 / 3) and s["avg_loss"] == pytest.approx(-75)
    assert s["expectancy"] == pytest.approx(40) and s["total"] == 200
    assert s["profit_factor"] == pytest.approx(350 / 150)
    assert s["max_drawdown"] == pytest.approx(100)         # equity 100,50,250,150,200 -> peak 250, trough 150


def test_edge_cases_no_losses_and_empty():
    s = fin._stats([10, 20])
    assert math.isinf(s["profit_factor"]) and s["max_drawdown"] == 0
    assert fin._stats([])["trades"] == 0
    assert fin.analyze([])["caveats"] == ["No trades to analyse."]


def test_streaks():
    assert fin._streaks([1, 1, 1, -1, -1, 1, -1, -1, -1, -1]) == {"max_win_streak": 3, "max_loss_streak": 4}


def test_small_samples_are_flagged_not_over_read():
    rep = fin.analyze([T(p) for p in [10, -5, 8, -4, 12]])
    assert any("Only 5 trades" in c for c in rep["caveats"])
    assert rep["interpretations"] == []


def test_detects_losses_after_two_wins_pattern_and_labels_it_a_hypothesis():
    # repeating W W L: every trade following two wins is a loser by construction
    pnls = [50, 50, -120] * 20
    base = datetime(2026, 1, 5, 9)
    trades = [T(p, base + timedelta(hours=i)) for i, p in enumerate(pnls)]
    rep = fin.analyze(trades)
    obs = " ".join(rep["observations"])
    assert "After two consecutive wins" in obs and "100% losers" in obs
    assert any("following two wins do noticeably worse" in i and "confidence" in i for i in rep["interpretations"])
    assert any("two consecutive wins" in r for r in rep["recommendations"])
    assert "not financial advice" in rep["disclaimer"]


def test_session_weekday_setup_breakdowns_need_a_minimum_bucket():
    base = datetime(2026, 1, 5, 9)           # a Monday
    trades = ([T(30, base + timedelta(days=7 * i), setup="breakout") for i in range(10)] +
              [T(-20, base + timedelta(days=7 * i, hours=1), setup="reversal") for i in range(10)] +
              [T(5, base, setup="rare")])
    rep = fin.analyze(trades)
    assert rep["tables"]["by_setup"]["breakout"]["win_rate"] == 1.0
    assert any("by setup" in o.lower() and "breakout" in o for o in rep["observations"])
    assert any("rare" in c and "too few" in c for c in rep["caveats"])


def test_risk_escalation_after_wins_is_noticed():
    base = datetime(2026, 1, 5, 9)
    trades = []
    for i in range(30):
        big = i % 3 == 2            # the trade after every two wins is 3x size and loses
        trades.append(T(-150 if big else 40, base + timedelta(hours=i * 2), size=3.0 if big else 1.0))
    rep = fin.analyze(trades)
    assert any("1.5× your median size" in o for o in rep["observations"])
    assert any("fixed risk-per-trade" in r for r in rep["recommendations"])


def test_calculators():
    rr = fin.risk_reward(2000, 1990, 2030)
    assert rr["reward_to_risk"] == pytest.approx(3.0) and rr["breakeven_win_rate"] == pytest.approx(0.25)
    assert fin.risk_reward(2000, 2010, 1970, "short")["reward_to_risk"] == pytest.approx(3.0)
    ps = fin.position_size(10_000, 1, 2000, 1990)
    assert ps["risk_amount"] == pytest.approx(100) and ps["position_size"] == pytest.approx(10)
    for bad in [lambda: fin.risk_reward(2000, 2010, 2030), lambda: fin.position_size(1000, 1, 5, 5), lambda: fin.position_size(0, 1, 5, 4)]:
        with pytest.raises(ValueError):
            bad()


def test_csv_loading_flexible_columns_money_formats_and_bad_rows(tmp_path):
    f = tmp_path / "j.csv"
    f.write_text("Open Time,Symbol,Direction,Profit,Lots,Strategy\n"
                 "2026-03-02 08:15,XAUUSD,buy,\"$1,200.50\",0.5,breakout\n"
                 "2026-03-02 14:30,XAUUSD,sell,(300.25),0.5,reversal\n"
                 "2026-03-03 09:00,EURUSD,buy,,0.1,breakout\n"
                 "2026-03-03 15:00,XAUUSD,buy,45,0.5,breakout\n")
    trades, warns = fin.load_trades(f)
    assert [t.pnl for t in trades] == [1200.50, -300.25, 45.0]
    assert trades[0].session == "London" and trades[1].session == "New York"
    assert any("1 row(s) skipped" in w for w in warns)
    with pytest.raises(ValueError):
        (tmp_path / "bad.csv").write_text("a,b\n1,2\n"); fin.load_trades(tmp_path / "bad.csv")


def test_xlsx_journal_and_tool_end_to_end(svc):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Date", "Symbol", "PnL", "Setup"])
    base = datetime(2026, 2, 2, 10)
    for i, p in enumerate([100, -50, 200, -100, 50]):
        ws.append([base + timedelta(days=i), "XAUUSD" if i != 3 else "EURUSD", p, "A"])
    path = svc.config.home / "Finance" / "journal.xlsx"; wb.save(path)
    res, _ = run(svc, "finance_journal_analyze", {"path": "Finance/journal.xlsx", "symbol": "XAUUSD"})
    assert res.ok and res.data["report"]["n"] == 4 and res.data["report"]["stats"]["total"] == 300  # 100-50+200+50; the EURUSD trade is excluded
    last2, _ = run(svc, "finance_journal_analyze", {"path": "Finance/journal.xlsx", "last": 2})
    assert last2.data["report"]["n"] == 2
    assert run(svc, "finance_journal_analyze", {"path": "Finance/journal.xlsx", "symbol": "GBPJPY"})[0].status == "failed"


def test_finance_calc_tool_rejects_nonsense(svc):
    assert run(svc, "finance_calc", {"kind": "risk_reward", "entry": 10, "stop": 12, "target": 15})[0].status == "failed"
    assert run(svc, "finance_calc", {"kind": "position_size", "entry": 10, "stop": 9, "balance": 5000, "risk_percent": 2})[0].ok


def test_spreadsheet_revenue_for_a_month(svc):
    (svc.config.home / "Finance").mkdir(exist_ok=True)
    (svc.config.home / "Finance" / "sales.csv").write_text(
        "Date,Client,Revenue\n2026-09-30,A,\"RM 900\"\n2026-10-01,A,\"RM 1,200.50\"\n2026-10-15,B,300\n2026-10-20,A,n/a\nnot-a-date,B,50\n")
    res, _ = run(svc, "spreadsheet_calc", {"path": "Finance/sales.csv", "column": "Revenue", "op": "sum",
                                           "date_column": "Date", "month": "2026-10"})
    assert res.ok and res.data["value"] == pytest.approx(1500.50) and res.data["rows_used"] == 3
    assert any("no readable date" in n for n in res.data["notes"]) and any("no numeric value" in n for n in res.data["notes"])
    by, _ = run(svc, "spreadsheet_calc", {"path": "Finance/sales.csv", "column": "Revenue", "group_by": "Client"})
    assert by.data["groups"]["A"] == pytest.approx(2100.5)
    summ, _ = run(svc, "spreadsheet_summary", {"path": "Finance/sales.csv"})
    assert summ.data["rows"] == 5 and "Revenue" in summ.data["numeric_columns"]
    assert run(svc, "spreadsheet_calc", {"path": "Finance/sales.csv", "column": "Nope"})[0].status == "failed"
