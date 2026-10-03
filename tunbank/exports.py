"""Excel exports for ECON. Every export records who asked for it (audit log)."""
from __future__ import annotations

import io

from . import ledger as L
from . import money as M
from . import reconcile as R
from .valuation import value_amounts

KINDS = ("balances", "ledger", "deposits", "withdrawals", "locks", "tax", "audit", "integrity", "vault", "offshore", "grants")


def _safe(v):
    """Stop spreadsheet formula injection: text starting with = + - @ is shown as text."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _num(units):
    return None if units is None else units / M.SCALE


def _sheet(wb, title, headers, rows):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for r in rows:
        ws.append([_safe(c) for c in r])
    ws.freeze_panes = "A2"
    for col in ws.columns:
        width = max((len(str(c.value)) if c.value is not None else 0) for c in list(col)[:200])
        ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)
    return ws


def build(conn, kind: str, snapshot, period: str = "all") -> tuple[bytes, str]:
    from openpyxl import Workbook

    if kind not in KINDS:
        raise ValueError(f"Unknown export '{kind}'")
    wb = Workbook()
    wb.remove(wb.active)
    res_cols = list(M.RESOURCES)

    if kind == "balances":
        rows = []
        nations = [r[0] for r in conn.execute("SELECT DISTINCT nation_id FROM balances ORDER BY nation_id")]
        for n in nations:
            m = L.get_member(conn, n)
            av, lk = L.get_balances(conn, n, "AVAILABLE"), L.get_balances(conn, n, "LOCKED")
            va, vl = value_amounts(av, snapshot), value_amounts(lk, snapshot)
            rows.append([n, m["nation_name"] if m else "", m["discord_id"] if m else "",
                         "yes" if m and m["frozen"] else "",
                         _num(va.total_cents), _num(vl.total_cents),
                         _num((va.total_cents or 0) + (vl.total_cents or 0)),
                         *[_num(av.get(r, 0)) for r in res_cols], *[_num(lk.get(r, 0)) for r in res_cols]])
        _sheet(wb, "Balances", ["Nation ID", "Nation", "Discord ID", "Frozen",
                                "Available value $", "Locked value $", "Total value $",
                                *[f"Avail {r}" for r in res_cols], *[f"Locked {r}" for r in res_cols]], rows)
        note = [["Prices as of", snapshot.fetched_at if snapshot else "UNAVAILABLE"],
                ["Price snapshot id", snapshot.id if snapshot else ""],
                ["Stale", "YES" if snapshot and snapshot.stale else "no"]]
        _sheet(wb, "Valuation info", ["Item", "Value"], note)
    elif kind == "ledger":
        rows = [[r["id"], r["ts"], r["nation_id"], r["bucket"], r["resource"], _num(r["delta"]),
                 r["entry_type"], r["pnw_record_id"], r["tx_id"], r["lock_id"], r["batch_id"],
                 r["adjustment_id"], r["actor"], r["note"], r["group_id"], r["price_snapshot_id"]]
                for r in conn.execute("SELECT * FROM ledger_entries ORDER BY id")]
        _sheet(wb, "Ledger", ["ID", "Time", "Nation", "Bucket", "Resource", "Change", "Type", "PnW record",
                              "Tx", "Lock", "Import batch", "Adjustment", "Actor", "Note", "Group",
                              "Price snapshot"], rows)
    elif kind == "deposits":
        rows = [[r["id"], r["record_date"], r["sender_id"], r["classification"], r["status"],
                 r["credited_nation_id"], r["note"], r["amounts_json"], r["price_snapshot_id"]]
                for r in conn.execute("SELECT * FROM pnw_records WHERE direction='IN' ORDER BY id")]
        _sheet(wb, "PnW inbound", ["PnW record", "Date", "Sender nation", "Classification", "Status",
                                   "Credited to", "Note", "Amounts (units/100)", "Price snapshot"], rows)
    elif kind == "withdrawals":
        rows = [[r["id"], r["created_at"], r["tx_type"], r["status"], r["funding_source"],
                 r["member_nation_id"], r["dest_nation_id"], r["actor_discord_id"],
                 r["approver_discord_id"], _num(r["value_cents"]), r["pnw_record_id"], r["note"],
                 r["reason"], r["failure_reason"]]
                for r in conn.execute("SELECT * FROM transactions ORDER BY id")]
        _sheet(wb, "Transactions", ["Tx", "Created", "Type", "Status", "Funding", "Member", "Destination",
                                    "Actor", "Approver", "Value $ at time", "PnW record", "Note", "Reason",
                                    "Failure"], rows)
    elif kind == "locks":
        rows = []
        for r in conn.execute("SELECT * FROM locks ORDER BY id"):
            rem = L.lock_remaining(conn, r["id"])
            rows.append([r["id"], r["nation_id"], r["lock_type"], r["reason"], r["created_by"],
                         r["created_at"], *[_num(rem.get(x, 0)) for x in res_cols]])
        _sheet(wb, "Locks", ["Lock", "Nation", "Type", "Reason", "By", "Created",
                             *[f"Remaining {x}" for x in res_cols]], rows)
    elif kind == "tax":
        from . import taxutil as TX

        rs = TX.rows(conn, period)
        exempt = TX.active_exemptions(conn)
        names = {r["nation_id"]: r["nation_name"] for r in conn.execute("SELECT nation_id, nation_name FROM members")}
        grouped = TX.by_nation(rs)
        rows = []
        for nid, g in sorted(grouped.items()):
            v = value_amounts(g["amounts"], snapshot)
            rows.append([nid, names.get(nid) or "", "yes (TUN policy)" if nid in exempt else "", TX.PERIOD_LABEL[period],
                         g["count"], _num(v.total_cents), *[_num(g["amounts"].get(x, 0)) for x in res_cols],
                         ", ".join(str(b) for b in sorted(g["brackets"])), ", ".join(str(x) for x in g["records"][:40])])
        _sheet(wb, "By member", ["Nation ID", "Nation", "Exempt", "Period", "Tax records", "Value now $",
                                 *[f"{x}" for x in res_cols], "Bracket id(s)", "PnW record ids (first 40)"], rows)
        _sheet(wb, "Records", ["ID", "PnW record", "Date", "Nation", "Bracket id", "Value now $", *res_cols],
               [[r["id"], r["pnw_record_id"], r["date"], r["nation_id"], r["tax_id"],
                 _num(value_amounts(r["amounts"], snapshot).total_cents),
                 *[_num(r["amounts"].get(x, 0)) for x in res_cols]] for r in rs])
        _sheet(wb, "Info", ["Item", "Value"], [
            ["Period", TX.PERIOD_LABEL[period]], ["Prices as of", snapshot.fetched_at if snapshot else "UNAVAILABLE"],
            ["Note", "Taxes are alliance-owned. 'Exempt' is TUN bookkeeping only; it does not change what PnW collects."]])
    elif kind == "offshore":
        rows = [[r["id"], r["created_at"], r["direction"], r["mode"], r["status"], r["actor"], r["reason"], _num(r["value_cents"]),
                 r["pnw_record_id"], r["failure_reason"], r["amounts_json"]]
                for r in conn.execute("SELECT * FROM offshore_transfers ORDER BY id")]
        _sheet(wb, "Offshore transfers", ["ID", "Created", "Direction", "Mode", "Status", "By", "Reason", "Value $ at time",
                                          "PnW record", "Failure", "Amounts (units/100)"], rows)
    elif kind == "grants":
        rows = [[r["id"], r["created_at"], r["recipient_nation_id"], r["purpose"], r["project"], r["requested_by"], r["approver"],
                 r["status"], _num(r["value_cents"]), r["tx_id"], r["pnw_record_id"], r["amounts_json"]]
                for r in conn.execute("SELECT * FROM grants ORDER BY id")]
        _sheet(wb, "Grants", ["ID", "Date", "Recipient", "Purpose", "Project", "Requested by", "Approver", "Status",
                              "Value $ at time", "Tx", "PnW record", "Amounts (units/100)"], rows)
    elif kind == "audit":
        rows = [[r["id"], r["ts"], r["actor"], r["action"], r["target"], r["details_json"]]
                for r in conn.execute("SELECT * FROM audit_log ORDER BY id")]
        _sheet(wb, "Audit log", ["ID", "Time", "Actor", "Action", "Target", "Details"], rows)
    elif kind == "integrity":
        rows = [[r["id"], r["ts"], r["severity"], r["kind"], r["nation_id"], r["status"], r["details_json"],
                 r["resolved_by"], r["resolved_at"], r["resolution_note"]]
                for r in conn.execute("SELECT * FROM integrity_events ORDER BY id")]
        _sheet(wb, "Integrity events", ["ID", "Time", "Severity", "Kind", "Nation", "Status", "Details",
                                        "Resolved by", "Resolved at", "Note"], rows)
    elif kind == "vault":
        last = conn.execute("SELECT bank_json, started_at FROM reconciliation_runs ORDER BY id DESC LIMIT 1").fetchone()
        import json
        pos = json.loads(last["bank_json"]) if last and last["bank_json"] else {}
        rows = []
        for r in res_cols:
            g = lambda k: (pos.get(k) or {}).get(r, 0)  # noqa: E731
            rows.append([r, _num(g("bank")), _num(g("available")), _num(g("locked")), _num(g("member_total")),
                         _num(g("alliance_owned"))])
        _sheet(wb, "Vault", ["Resource", "PnW bank", "Member available", "Member locked", "Member total",
                             "Alliance-owned"], rows)
        _sheet(wb, "Info", ["Item", "Value"], [["From reconciliation run at", last["started_at"] if last else "never"]])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"tunbank_{kind}.xlsx"
