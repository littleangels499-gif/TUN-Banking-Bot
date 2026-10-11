"""The reconciliation report ECON reads. Says what is wrong and what to do, not just an error code."""
from __future__ import annotations

from . import alerts as A
from . import fmt
from . import icons
from . import money as M

STATUS = {
    "NORMAL": ("🟢", "NORMAL", "Fully reconciled.", A.GREEN),
    "WARNING": ("🟡", "WARNING", "A resource-level discrepancy, but no integrity problem. Banking continues.", A.ORANGE),
    "RECONCILIATION_REQUIRED": ("🟠", "RECONCILIATION REQUIRED",
                                "An unexplained discrepancy that needs ECON investigation.", A.ORANGE),
    "EMERGENCY_LOCK": ("🔴", "EMERGENCY LOCK", "A serious integrity problem. Financial changes are halted.", A.RED),
}
SEV_ICON = {"CRITICAL": "🔴", "RECON": "🟠", "WARNING": "🟡"}


def _f(res: str):
    return fmt.compact_money if res == "money" else fmt.compact_number


def resource_line(res: str, p: dict) -> str:
    f = _f(res)
    diff = p["difference"]
    sign = "+" if diff > 0 else ""
    owed = f" _(net of {f(p['owed'])} owed to the alliance)_" if p["owed"] else ""
    return (f"{icons.resource(res)} **{M.LABELS[res]}** · Bank {f(p['bank'])} · Members {f(p['member_net'])}{owed}"
            f" · Difference **{sign}{f(diff)}** {'⚠️' if diff < 0 else '✓'}")


def _chunks(lines: list[str], limit: int = 900) -> list[str]:
    out, cur = [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + ln
    return out + ([cur] if cur else [])


def what_to_do(status: str, short: list[str], lock_reason: str = "") -> str:
    if status == "NORMAL":
        return "Nothing to do."
    if status == "WARNING":
        return ("**No action is needed to keep banking running.** Members can still withdraw any resource the bank holds; "
                "a withdrawal of a short resource is refused automatically.\n"
                + ("To clear the warning for " + ", ".join(short) + ": check `/bank review` for deposits not yet credited, "
                   "move the resource into the bank, or correct members' balances with `/bank adjust` if they are wrong."
                   if short else "Review the findings above."))
    if status == "RECONCILIATION_REQUIRED":
        return ("**Withdrawals, conversions and imports are paused until this is cleared.**\n"
                "1. `/bank review` – deal with any unprocessed PnW records.\n"
                "2. `/bank records balances` – compare member balances with the real bank.\n"
                "3. Fix the cause, then run `/ledger reconcile` again. Position findings clear themselves once the numbers "
                "agree; anything else is closed with `/ledger resolve` and a note.")
    return ("**All financial changes are halted.** Run `/ledger dashboard` to see exactly why"
            + (f" (lock reason: {lock_reason})" if lock_reason else "")
            + ". Nothing was rewritten. When the cause is understood, resolve the events with `/ledger resolve` "
              "and lift the lock with `/bank emergency`.")


def report_card(result: dict, *, show_figures: bool) -> A.Card:
    status = result.get("status") or ("NORMAL" if result["result"] == "OK" else "WARNING")
    emoji, label, meaning, color = STATUS.get(status, STATUS["WARNING"])
    c = A.Card("TUN BANK RECONCILIATION", f"{emoji} **{label}** — {meaning}", color)
    pos = result["position"]
    res = pos.get("resources")
    short = [M.LABELS[r] for r, p in (res or {}).items() if p["difference"] < 0]
    net = pos.get("net_cents")
    if res is None:
        c.add("Resource position", "The real PnW bank could not be read, so nothing could be compared right now.")
    elif show_figures:
        ordered = sorted(res.items(), key=lambda kv: (kv[1]["difference"] >= 0, M.RESOURCES.index(kv[0])))
        for n, text in enumerate(_chunks([resource_line(r, p) for r, p in ordered])):
            c.add("Resource position" if n == 0 else "Resource position (continued)", text)
        if net is None:
            miss = ", ".join(M.LABELS[r] for r in pos.get("missing_prices", [])) or "prices"
            c.add("Overall net market position", f"Unavailable (no reliable price for {miss}).")
        else:
            c.add("Overall net market position", f"**{fmt.compact_money(net)}** {'✓ positive' if net >= 0 else '⚠️ NEGATIVE'}"
                  "  _(every difference valued at current market prices)_")
    else:
        c.add("Resource position", ("⚠️ Short in the bank: " + ", ".join(short)) if short else "✓ Every resource is covered.")
        c.add("Overall net market position", "Unavailable" if net is None else ("✓ positive" if net >= 0 else "⚠️ NEGATIVE"))
    off = pos.get("offshore")
    if off and show_figures:
        an = off["analysis"]
        mon = lambda r, v: (fmt.compact_money if r == "money" else fmt.compact_number)(v)   # noqa: E731
        lines = [f"{'🏠' if o['is_host'] else '🤝'} **{o['name']}** [#{o['alliance_id']}] · "
                 + (", ".join(f"{M.LABELS[r]} {mon(r, v)}" for r, v in o["balances"].items()) or "nothing") for o in off["owners"]]
        c.add("🏝️ Shared offshore · recorded ownership", "\n".join(lines)[:1000])
        # A. resource level: shortages and surpluses side by side, never netted
        rl = []
        for r, v in sorted(an["resources"].items(), key=lambda kv: (kv[1]["state"] != "SHORT", kv[1]["state"] != "EXCESS", M.RESOURCES.index(kv[0]))):
            tag = {"SHORT": "⚠️ owned > held", "EXCESS": "➕ held > owned (unassigned)", "OK": "✓"}[v["state"]]
            rl.append(f"{icons.resource(r)} **{M.LABELS[r]}** · Held {mon(r, v['physical'])} · Owned {mon(r, v['owned'])} · "
                      f"Diff {'+' if v['difference'] > 0 else ''}{mon(r, v['difference'])} {tag}")
        for n, t in enumerate(_chunks(rl)):
            c.add("A · Offshore, resource by resource" if n == 0 else "A · (continued)", t)
        # B. overall market value
        if an["net_cents"] is None:
            c.add("B · Offshore, overall market value", "Unavailable (no reliable price for " + (", ".join(M.LABELS[r] for r in an["missing_prices"]) or "a resource") + ").")
        else:
            c.add("B · Offshore, overall market value",
                  f"Held **{fmt.compact_money(an['value_physical'])}** vs recorded ownership **{fmt.compact_money(an['value_owned'])}** → "
                  f"difference **{fmt.compact_money(an['net_cents'])}** {'✓' if an['net_cents'] >= 0 else '⚠️'}")
        # C. unexplained transactions
        if off["unexplained"]:
            ul = [f"#{u['id']} · {'IN ' if u['inbound'] else 'OUT'} · "
                  + ", ".join(f"{M.LABELS[r]} {mon(r, v)}" for r, v in list(u["amounts"].items())[:3])
                  + f" · {'from' if u['inbound'] else 'to'} {u['sender_id'] if u['inbound'] else u['receiver_id']}" for u in off["unexplained"][:8]]
            c.add(f"C · Unexplained offshore transactions ({len(off['unexplained'])})",
                  "\n".join(ul)[:900] + "\nNot assigned to anyone. Investigate, then `/offshore attribute`.")
        else:
            c.add("C · Unexplained offshore transactions", "None ✓")
    c.add("Liquidity", "Checked separately for every withdrawal: it is refused only if the bank that pays does not "
                       "physically hold the requested resource. A short resource does not stop other withdrawals.")
    for f in result["findings"][:8]:
        c.add(f"{SEV_ICON.get(f['severity'], '•')} {f['kind'].replace('_', ' ').title()}", f["message"][:400])
    c.add("What to do", what_to_do(status, short, result.get("lock_reason", "")))
    c.add("Run", f"#{result['run_id']}", True)
    c.add("Status", f"{emoji} {label}", True)
    return c
