"""Charts drawn from the bot's REAL data (ledger, PnW records, reconciliation runs).

Every chart returns PNG bytes. Values use the central valuation engine; deposits and
taxes use the prices recorded WHEN they happened when available. Trend charts say
clearly which prices they use. Nothing is invented: no data -> ChartError.
"""
from __future__ import annotations

import datetime as dt
import io
import json
from decimal import Decimal

from . import money as M
from .config import cfg_int  # noqa: F401  (kept for future chart settings)
from .util import ISO_FMT, utcnow
from .valuation import Snapshot, value_amounts


class ChartError(Exception):
    """Friendly message: why a chart can't be drawn."""


BG, PANEL, TEXT, MUTED, GRID = "#1e1f22", "#2b2d31", "#f2f3f5", "#b5bac1", "#3f4147"
RES_COLORS = {
    "money": "#57f287", "food": "#e8b05a", "coal": "#8a8d93", "oil": "#4f545c", "uranium": "#a6e22e",
    "iron": "#c0c4cc", "bauxite": "#b5651d", "lead": "#7289da", "gasoline": "#ed4245", "munitions": "#fee75c",
    "steel": "#99aab5", "aluminum": "#5865f2",
}
ACCENT, ACCENT2, ACCENT3 = "#5865f2", "#57f287", "#fee75c"


def _plt():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except ImportError as exc:  # pragma: no cover
        raise ChartError("Charts need the 'matplotlib' package. Run: pip install -r requirements.txt") from exc


def _money_fmt(v, _pos=None):
    a = abs(v)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= limit:
            return f"${v / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return f"${v:,.0f}"


def _figure(title: str, subtitle: str = "", size=(8, 4.8)):
    plt = _plt()
    fig, ax = plt.subplots(figsize=size, dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.yaxis.label.set_color(MUTED)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)
    fig.text(0.04, 0.955, title, color=TEXT, fontsize=15, fontweight="bold", va="top")
    if subtitle:
        fig.text(0.04, 0.895, subtitle, color=MUTED, fontsize=9, va="top")
    fig.text(0.96, 0.02, "TUN Bank", color=GRID, fontsize=8, ha="right")
    return fig, ax


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    _plt().close(fig)
    return buf.getvalue()


# -------------------------------------------------------------- helpers
def _snapshot_for(conn, sid, cache: dict, fallback: Snapshot | None):
    if sid is None:
        return fallback
    if sid not in cache:
        row = conn.execute("SELECT * FROM price_snapshots WHERE id=?", (sid,)).fetchone()
        cache[sid] = (Snapshot(row["id"], row["fetched_at"],
                               {k: Decimal(v) for k, v in json.loads(row["prices_json"]).items()})
                      if row else fallback)
    return cache[sid]


def _cents(res: str, units: int, snap: Snapshot | None) -> tuple[int, bool]:
    """(value in cents, priced?)"""
    v = value_amounts({res: units}, snap)
    if v.total_cents is None:
        return 0, False
    return v.total_cents, True


def _day(text: str | None) -> str | None:
    if not text:
        return None
    t = str(text).strip()
    return t[:10] if len(t) >= 10 and t[4] == "-" and t[7] == "-" else None


def _days_axis(ax, days: list[str]):
    labels = [d[5:] for d in days]
    step = max(1, len(days) // 8)
    ax.set_xticks(range(0, len(days), step))
    ax.set_xticklabels([labels[i] for i in range(0, len(days), step)], rotation=0)


def _fill_days(by_day: dict, window: int) -> list[str]:
    end = utcnow().date()
    return [(end - dt.timedelta(days=i)).isoformat() for i in range(window - 1, -1, -1)]


# ---------------------------------------------------------------- charts
def composition(conn, snap: Snapshot | None, nation_id: int | None = None, label: str = "") -> bytes:
    """Donut: share of market value per resource (member-held funds)."""
    if snap is None:
        raise ChartError("No price data yet, so values can't be drawn. Try again in a minute.")
    if nation_id is None:
        rows = conn.execute("SELECT resource, SUM(amount) a FROM balances GROUP BY resource").fetchall()
    else:
        rows = conn.execute("SELECT resource, SUM(amount) a FROM balances WHERE nation_id=? GROUP BY resource",
                            (nation_id,)).fetchall()
    parts, unpriced = [], []
    for r in rows:
        if not r["a"]:
            continue
        c, ok = _cents(r["resource"], r["a"], snap)
        (parts if ok else unpriced).append((r["resource"], c))
    parts = [(r, c) for r, c in parts if c > 0]
    if not parts:
        raise ChartError("There are no funds to draw yet.")
    parts.sort(key=lambda kv: -kv[1])
    total = sum(c for _, c in parts)
    plt = _plt()
    fig, ax = _figure("Resource composition", f"{label or 'All member-held funds'} · by Current Market Value "
                      f"(prices as of {snap.fetched_at})", size=(8, 4.8))
    ax.grid(False)
    ax.set_facecolor(BG)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.set_position([0.02, 0.05, 0.5, 0.78])
    wedges, _ = ax.pie([c for _, c in parts], colors=[RES_COLORS[r] for r, _ in parts], startangle=90,
                       counterclock=False, wedgeprops=dict(width=0.38, edgecolor=BG, linewidth=2))
    ax.text(0, 0.07, _money_fmt(total / 100), ha="center", va="center", color=TEXT, fontsize=17, fontweight="bold")
    ax.text(0, -0.15, "total value", ha="center", va="center", color=MUTED, fontsize=9)
    legend_lines = [f"{M.LABELS[r]:<10}  {_money_fmt(c / 100):>8}   {c / total * 100:4.1f}%" for r, c in parts[:12]]
    handles = [plt.Line2D([0], [0], marker="s", color="none", markerfacecolor=RES_COLORS[r], markersize=10)
               for r, _ in parts[:12]]
    leg = fig.legend(handles, legend_lines, loc="center left", bbox_to_anchor=(0.54, 0.46), frameon=False,
                     labelcolor=TEXT, prop={"family": "monospace", "size": 9.5})
    leg.set_title("")
    if unpriced:
        fig.text(0.04, 0.02, "No price for: " + ", ".join(r for r, _ in unpriced), color="#fee75c", fontsize=8)
    return _png(fig)


def deposits(conn, snap: Snapshot | None, days: int = 30, nation_id: int | None = None, label: str = "") -> bytes:
    """Bars: value of deposits credited per day (valued at the prices recorded at the time)."""
    since = (utcnow() - dt.timedelta(days=days)).strftime(ISO_FMT)
    q = "SELECT ts, resource, delta, price_snapshot_id FROM ledger_entries WHERE entry_type='DEPOSIT' AND ts>=?"
    args = [since]
    if nation_id is not None:
        q += " AND nation_id=?"
        args.append(nation_id)
    rows = conn.execute(q, args).fetchall()
    if not rows:
        raise ChartError(f"No deposits in the last {days} days.")
    cache: dict = {}
    by_day: dict = {}
    unpriced = 0
    for r in rows:
        c, ok = _cents(r["resource"], r["delta"], _snapshot_for(conn, r["price_snapshot_id"], cache, snap))
        unpriced += 0 if ok else 1
        d = _day(r["ts"])
        by_day[d] = by_day.get(d, 0) + c
    axis = _fill_days(by_day, days)
    vals = [by_day.get(d, 0) / 100 for d in axis]
    fig, ax = _figure("Deposits per day", f"{label or 'All members'} · last {days} days · valued at the prices "
                      "recorded when each deposit arrived")
    ax.bar(range(len(axis)), vals, color=ACCENT2, width=0.72)
    ax.yaxis.set_major_formatter(_money_fmt_formatter())
    _days_axis(ax, axis)
    ax.set_xlim(-0.8, len(axis) - 0.2)
    total = sum(vals)
    fig.text(0.96, 0.955, f"Total {_money_fmt(total)}", color=ACCENT2, fontsize=12, fontweight="bold", ha="right", va="top")
    if unpriced:
        fig.text(0.04, 0.02, f"{unpriced} line(s) had no price and are not counted", color="#fee75c", fontsize=8)
    fig.subplots_adjust(top=0.82, bottom=0.12, left=0.11, right=0.97)
    return _png(fig)


def tax(conn, snap: Snapshot | None, days: int = 30) -> bytes:
    """Bars: tax collected per day (alliance-owned). Staff only."""
    rows = conn.execute("SELECT record_date, recorded_at, amounts_json, price_snapshot_id FROM tax_records").fetchall()
    cutoff = (utcnow() - dt.timedelta(days=days)).date().isoformat()
    cache: dict = {}
    by_day: dict = {}
    unpriced = 0
    for r in rows:
        d = _day(r["record_date"]) or _day(r["recorded_at"])
        if not d or d < cutoff:
            continue
        sn = _snapshot_for(conn, r["price_snapshot_id"], cache, snap)
        for res, units in json.loads(r["amounts_json"]).items():
            c, ok = _cents(res, units, sn)
            unpriced += 0 if ok else 1
            by_day[d] = by_day.get(d, 0) + c
    if not by_day:
        raise ChartError(f"No tax records in the last {days} days.")
    axis = _fill_days(by_day, days)
    vals = [by_day.get(d, 0) / 100 for d in axis]
    fig, ax = _figure("Tax collected per day", f"Alliance-owned · last {days} days · valued at the prices recorded at the time")
    ax.bar(range(len(axis)), vals, color=ACCENT3, width=0.72)
    ax.yaxis.set_major_formatter(_money_fmt_formatter())
    _days_axis(ax, axis)
    ax.set_xlim(-0.8, len(axis) - 0.2)
    fig.text(0.96, 0.955, f"Total {_money_fmt(sum(vals))}", color=ACCENT3, fontsize=12, fontweight="bold", ha="right", va="top")
    if unpriced:
        fig.text(0.04, 0.02, f"{unpriced} line(s) had no price and are not counted", color="#fee75c", fontsize=8)
    fig.subplots_adjust(top=0.82, bottom=0.12, left=0.11, right=0.97)
    return _png(fig)


def vault(conn, snap: Snapshot | None) -> bytes:
    """Stacked bars per resource: member available / member locked / alliance-owned (latest reconciliation)."""
    if snap is None:
        raise ChartError("No price data yet, so values can't be drawn.")
    row = conn.execute("SELECT bank_json, started_at FROM reconciliation_runs WHERE bank_json IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise ChartError("No reconciliation has run yet. Run /ledger reconcile first.")
    pos = json.loads(row["bank_json"])
    if pos.get("bank") is None or pos.get("alliance_owned") is None:
        raise ChartError("The last reconciliation couldn't read the real PnW bank, so alliance-owned funds are unknown.")
    series = {"Member available": pos.get("available", {}), "Member locked": pos.get("locked", {}),
              "Alliance-owned": {k: v for k, v in pos["alliance_owned"].items() if v > 0}}
    resources = [r for r in M.RESOURCES if any(s.get(r) for s in series.values())]
    if not resources:
        raise ChartError("The vault is empty.")
    colors = {"Member available": ACCENT, "Member locked": "#eb459e", "Alliance-owned": ACCENT2}
    fig, ax = _figure("Alliance vs member-held funds", f"By resource · Current Market Value · reconciliation of "
                      f"{row['started_at']} · prices as of {snap.fetched_at}", size=(8, 5))
    left = [0.0] * len(resources)
    for name, data in series.items():
        vals = [(_cents(r, data.get(r, 0), snap)[0] / 100) if data.get(r) else 0 for r in resources]
        ax.barh(range(len(resources)), vals, left=left, color=colors[name], label=name, height=0.62)
        left = [a + b for a, b in zip(left, vals)]
    ax.set_yticks(range(len(resources)))
    ax.set_yticklabels([M.LABELS[r] for r in resources])
    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(_money_fmt_formatter())
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right", frameon=False, labelcolor=TEXT, fontsize=9)
    fig.subplots_adjust(top=0.82, bottom=0.1, left=0.17, right=0.97)
    return _png(fig)


def wealth(conn, snap: Snapshot | None, nation_id: int, label: str = "") -> bytes:
    """Line: a member's total deposit value over time (valued at CURRENT prices)."""
    if snap is None:
        raise ChartError("No price data yet, so values can't be drawn.")
    rows = conn.execute("SELECT ts, resource, delta FROM ledger_entries WHERE nation_id=? ORDER BY id", (nation_id,)).fetchall()
    if not rows:
        raise ChartError("This account has no history yet.")
    held: dict = {}
    by_day: dict = {}
    for r in rows:
        held[r["resource"]] = held.get(r["resource"], 0) + r["delta"]
        v = value_amounts({k: u for k, u in held.items() if u > 0}, snap)
        by_day[_day(r["ts"])] = (v.total_cents or 0) / 100
    days = sorted(by_day)
    start = dt.date.fromisoformat(days[0])
    end = utcnow().date()
    axis, last = [], 0.0
    cur = start
    series = []
    while cur <= end:
        iso = cur.isoformat()
        last = by_day.get(iso, last)
        axis.append(iso)
        series.append(last)
        cur += dt.timedelta(days=1)
    fig, ax = _figure("Account value over time", f"{label or 'Member'} · deposits + locked funds · "
                      f"valued at CURRENT prices ({snap.fetched_at})")
    if len(series) == 1:
        series, axis = series * 2, [axis[0], axis[0]]
    ax.plot(range(len(series)), series, color=ACCENT, linewidth=2.4)
    ax.fill_between(range(len(series)), series, color=ACCENT, alpha=0.18)
    ax.yaxis.set_major_formatter(_money_fmt_formatter())
    _days_axis(ax, axis)
    ax.set_xlim(0, max(1, len(series) - 1))
    ax.set_ylim(bottom=0)
    fig.text(0.96, 0.955, _money_fmt(series[-1]), color=ACCENT, fontsize=14, fontweight="bold", ha="right", va="top")
    fig.subplots_adjust(top=0.82, bottom=0.12, left=0.11, right=0.97)
    return _png(fig)


def _money_fmt_formatter():
    from matplotlib.ticker import FuncFormatter

    return FuncFormatter(_money_fmt)
