"""/help - category pages with buttons, built from the bot's REAL command list.

The command names and descriptions are read from the registered commands themselves, so /help can never
show something that doesn't exist. CATALOG says which category and permission level each command belongs
to; a test fails if any registered command is missing from it (so a new command can't be forgotten) or
if CATALOG mentions a command that no longer exists.
"""
from __future__ import annotations

import discord

from . import alerts as A
from . import perms
from .ui import Services, levels, reply

# ---- level badges
BADGE = {"ALL": "", "AUDITOR": "👁️ Auditor", "BANKER": "🏦 Banker", "MINISTER": "🛡️ Minister", "ADMIN": "👑 Admin",
         "FLAG:bank_view_alliance_holdings": "🔒 Treasury access", "FLAG:bank_view_tax": "🔒 Tax access"}

# ---- categories: key -> (button label, emoji, blurb)
CATEGORIES = {
    "start": ("Start here", "📖", ""),
    "account": ("My account", "🏦", "Everything you do with your own deposit."),
    "banking": ("Banking", "💸", "Day-to-day work for ECON staff: sending, reserving, correcting and reviewing."),
    "bulk": ("Bulk transfers", "📦", "Pay many nations at once from alliance-owned funds. Every row is checked first and never sent twice."),
    "tax": ("Tax", "🧾", "Tax collected from members. Alliance-owned; members never see this."),
    "audit": ("Audit & security", "🔎", "Checks, approvals and the emergency lock."),
    "charts": ("Charts", "📊", "Pictures drawn from the bank's real data."),
    "config": ("Configuration", "⚙️", "Roles, confidential-access permissions, limits, icons, channels, imports and backups."),
}

# ---- every command: "path" -> (category, minimum level)
CATALOG = {
    # account
    "nation link": ("account", "ALL"), "nation setkey": ("account", "ALL"), "nation removekey": ("account", "ALL"), "bank dashboard": ("account", "ALL"), "bank balance": ("account", "ALL"),
    "bank deposit": ("account", "ALL"), "bank withdrawself": ("account", "ALL"), "bank history": ("account", "ALL"),
    "chart mybalance": ("account", "ALL"), "chart mytrend": ("account", "ALL"),
    "prices": ("account", "ALL"), "help": ("account", "ALL"),
    # banking
    "bank holdings": ("banking", "FLAG:bank_view_alliance_holdings"), "bank transactions": ("banking", "AUDITOR"), "bank records": ("banking", "AUDITOR"),
    "bank scandeposits": ("banking", "BANKER"), "bank withdraw": ("banking", "BANKER"),
    "bank reserve": ("banking", "MINISTER"), "bank release": ("banking", "MINISTER"), "bank adjust": ("banking", "MINISTER"),
    "bank freeze": ("banking", "MINISTER"), "bank unfreeze": ("banking", "MINISTER"), "bank lock": ("banking", "MINISTER"),
    "bank unlock": ("banking", "MINISTER"), "bank review": ("banking", "MINISTER"), "bank resolvetx": ("banking", "MINISTER"),
    "bank offshore": ("banking", "BANKER"), "bank linknation": ("banking", "MINISTER"),
    "grant send": ("banking", "MINISTER"), "grant list": ("banking", "AUDITOR"), "grant view": ("banking", "AUDITOR"),
    # bulk
    "bulk template": ("bulk", "BANKER"), "bulk send": ("bulk", "BANKER"), "bulk resume": ("bulk", "BANKER"),
    "bulk status": ("bulk", "AUDITOR"),
    # tax
    "tax sync": ("tax", "BANKER"), "tax turns": ("tax", "FLAG:bank_view_tax"), "tax dashboard": ("tax", "FLAG:bank_view_tax"), "tax report": ("tax", "FLAG:bank_view_tax"),
    "tax paid": ("tax", "FLAG:bank_view_tax"), "tax profile": ("tax", "FLAG:bank_view_tax"), "tax brackets": ("tax", "FLAG:bank_view_tax"),
    "tax exemptions": ("tax", "FLAG:bank_view_tax"), "tax export": ("tax", "FLAG:bank_view_tax"),
    # audit & security
    "bank reconcile": ("audit", "AUDITOR"), "bank approvals": ("audit", "AUDITOR"), "bank approve": ("audit", "MINISTER"),
    "bank revoke": ("audit", "MINISTER"), "audit transactions": ("audit", "AUDITOR"), "audit stafflog": ("audit", "AUDITOR"),
    "audit nation": ("audit", "AUDITOR"), "audit run": ("audit", "AUDITOR"), "audit configlog": ("audit", "ADMIN"), "ledger reconcile": ("audit", "AUDITOR"),
    "ledger dashboard": ("audit", "AUDITOR"), "ledger emergencylock": ("audit", "MINISTER"), "ledger resolve": ("audit", "ADMIN"),
    "deposit reset": ("config", "ADMIN"), "deposit restore": ("config", "ADMIN"),
    # charts (staff)
    "chart nation": ("charts", "AUDITOR"), "chart vault": ("charts", "FLAG:bank_view_alliance_holdings"), "chart members": ("charts", "FLAG:bank_view_alliance_holdings"),
    "chart deposits": ("charts", "AUDITOR"), "chart tax": ("charts", "FLAG:bank_view_tax"),
    # configuration
    "bankset limits": ("config", "AUDITOR"), "bankset importopening": ("config", "ADMIN"), "bankset setaccess": ("config", "ADMIN"), "bankset access": ("config", "ADMIN"), "bankset listbankers": ("config", "AUDITOR"), "bankset icons": ("config", "ADMIN"),
    "bankset setrole": ("config", "ADMIN"), "bankset setlogchannel": ("config", "ADMIN"), "bankset config": ("config", "ADMIN"),
    "bankset seticon": ("config", "ADMIN"), "bankset addbanker": ("config", "ADMIN"), "bankset removebanker": ("config", "ADMIN"),
    "bankset settransferlimit": ("config", "ADMIN"), "bankset setdailylimit": ("config", "ADMIN"),
    "bankset setrolelimit": ("config", "ADMIN"), "bankset setnationlimit": ("config", "ADMIN"),
    "bankset requireapproval": ("config", "ADMIN"), "bankset backup": ("config", "ADMIN"), "bankset restorestage": ("config", "ADMIN"),
}
PER_PAGE = 7
ORDER = ["ALL", "AUDITOR", "BANKER", "MINISTER", "ADMIN", "FLAG:bank_view_alliance_holdings", "FLAG:bank_view_tax"]


def iter_commands(tree):
    """Yield (path, description) for every registered command, straight from the live command tree."""
    for item in tree.get_commands():
        kids = getattr(item, "commands", None)
        if kids is not None:                                    # a group like /bank
            for k in (kids.values() if isinstance(kids, dict) else kids):
                yield f"{item.name} {k.name}", k.description
        else:
            yield item.name, item.description


def clean(desc: str) -> str:
    for prefix in ("ECON: ", "Admin: ", "Administrator: "):
        if desc.startswith(prefix):
            desc = desc[len(prefix):]
    return desc[:1].upper() + desc[1:] if desc else desc


def visible_commands(tree, user_levels: set, category: str):
    out = []
    for path, desc in iter_commands(tree):
        cat, lvl = CATALOG.get(path, (None, None))
        if cat != category:
            continue
        if lvl == "ALL" or perms.has(user_levels, lvl):
            out.append((path, lvl, clean(desc)))
    return sorted(out, key=lambda x: (ORDER.index(x[1]), x[0]))


def category_cards(tree, user_levels: set, category: str) -> list:
    cmds = visible_commands(tree, user_levels, category)
    label, emoji, blurb = CATEGORIES[category]
    pages = []
    for i in range(0, len(cmds), PER_PAGE):
        lines = []
        for path, lvl, desc in cmds[i:i + PER_PAGE]:
            badge = f" · {BADGE[lvl]}" if BADGE[lvl] else ""
            lines.append(f"**/{path}**{badge}\n-# {desc}")
        pages.append(A.Card(f"{emoji} Help · {label}", blurb + "\n\n" + "\n\n".join(lines), A.BLUE))
    for n, c in enumerate(pages):
        c.footer = f"{label} · page {n + 1} of {len(pages)} · TUN Bank"
    return pages


def start_card(user_levels: set) -> A.Card:
    names = ["Member"] + [BADGE[k].split(" ", 1)[1] for k in ("AUDITOR", "BANKER", "MINISTER", "ADMIN") if perms.has(user_levels, k)]
    c = A.Card("📖 Welcome to TUN Bank", "Your balance is a record of **real deposits** to the alliance bank. "
               "Nothing is ever sent without a confirmation screen.", A.BLUE)
    c.add("🔑 Your access", " · ".join(names))
    c.add("Quick start", "1. `/nation link` - connect your nation\n2. `/bank dashboard` - see your account (it has buttons for withdrawing)\n"
                         "3. `/bank deposit` - learn how to deposit")
    c.add("Confidential", "Members never see the alliance's bank holdings. ECON staff see members' accounts; the alliance treasury and tax "
                          "need their own permission, which an Admin gives to specific roles.")
    c.add("Deposit notes", "No note = credited to you.\n`#ignore` = donation to the alliance (not credited)\n"
                           "`#loan repayment` = loan repayment (not credited)")
    c.add("Using this help", "Press a **category button** below to see its commands. Some categories have several pages: use ◀ ▶.")
    c.add("Problem?", "Ask ECON. If the bank says it is locked or under review, the books are being checked and your funds are safe.")
    c.footer = "Start here · TUN Bank"
    return c


class HelpView(discord.ui.View):
    def __init__(self, user_id: int, tree, user_levels: set, quick: list, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.user_id, self.tree, self.levels, self.quick = user_id, tree, user_levels, quick
        self.pages: dict = {"start": [start_card(user_levels)]}
        for key in CATEGORIES:
            if key != "start":
                cards = category_cards(tree, user_levels, key)
                if cards:
                    self.pages[key] = cards
        self.current, self.index = "start", 0
        self.handlers: dict = {}
        self._build()

    def card(self) -> A.Card:
        return self.pages[self.current][self.index]

    def _button(self, label, emoji, style, row, cb):
        b = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)

        async def handler(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message(
                    "This help screen belongs to someone else. Type /help for your own.", ephemeral=True)
            await cb(interaction)
        b.callback = handler
        self.handlers[label] = handler
        self.add_item(b)
        return b

    def _build(self):
        self.clear_items()
        self.handlers = {}
        for n, key in enumerate(self.pages):
            label, emoji, _ = CATEGORIES[key]
            style = discord.ButtonStyle.primary if key == self.current else discord.ButtonStyle.secondary
            self._button(label, emoji, style, n // 4, self._goto(key))
        pages = self.pages[self.current]
        if len(pages) > 1:
            prev = self._button("Previous page", "◀️", discord.ButtonStyle.secondary, 2, self._step(-1))
            nxt = self._button("Next page", "▶️", discord.ButtonStyle.secondary, 2, self._step(+1))
            prev.disabled = self.index <= 0
            nxt.disabled = self.index >= len(pages) - 1
        for label, emoji, cb in self.quick:
            self._button(label, emoji, discord.ButtonStyle.success, 3, cb)

    def _goto(self, key):
        async def go(interaction):
            self.current, self.index = key, 0
            self._build()
            await interaction.response.edit_message(embed=A.to_embed(self.card()), view=self)
        return go

    def _step(self, delta):
        async def go(interaction):
            self.index = max(0, min(len(self.pages[self.current]) - 1, self.index + delta))
            self._build()
            await interaction.response.edit_message(embed=A.to_embed(self.card()), view=self)
        return go


def register(tree, svc: Services):
    @tree.command(name="help", description="Show the TUN Bank commands you can use, by category")
    async def help_cmd(interaction: discord.Interaction):
        lv = levels(svc, interaction)

        async def q_dash(i):
            await svc.actions["dashboard"](i)

        async def q_prices(i):
            await svc.actions["prices"](i)

        async def q_vault(i):
            await svc.actions["holdings"](i)

        async def q_ledger(i):
            await svc.actions["ledger"](i)
        quick = [("My dashboard", "🏦", q_dash), ("Market prices", "💹", q_prices)]
        if perms.has(lv, "FLAG:bank_view_alliance_holdings"):
            quick.append(("Vault", "🏛️", q_vault))
        if perms.has(lv, "AUDITOR"):
            quick.append(("Integrity", "🛡️", q_ledger))
        view = HelpView(interaction.user.id, tree, lv, quick)
        await reply(interaction, card=view.card(), view=view)
