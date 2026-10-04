"""Shared Discord helpers: service container, permission checks, confirmation screens."""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import discord

from . import alerts as A
from . import ledger as L
from . import perms
from .resolve import ResolveError, resolve

log = logging.getLogger("tunbank.ui")


@dataclass
class Services:
    settings: object
    db: object
    pnw: object
    prices: object
    scanner: object
    wd: object
    alerts: object
    actions: dict = field(default_factory=dict)   # command callbacks that buttons can trigger
    offshore: object = None
    crypto: object = None
    deposits: object = None


def actor_label(interaction: discord.Interaction) -> str:
    return f"{interaction.user} ({interaction.user.id})"


def role_ids(interaction: discord.Interaction):
    return [r.id for r in getattr(interaction.user, "roles", [])]


def levels(svc: Services, interaction: discord.Interaction) -> set:
    with svc.db.read() as conn:
        return perms.levels_for(conn, interaction.user.id, role_ids(interaction), svc.settings.owner_ids)


def icons_lock() -> str:
    from . import icons
    return icons.status("lock")


def has_flag(svc: Services, interaction: discord.Interaction, flag: str) -> bool:
    return ("FLAG:" + flag) in levels(svc, interaction)


async def need(svc: Services, interaction: discord.Interaction, level: str) -> bool:
    """True if allowed. Otherwise answers the user privately and returns False."""
    from . import configaudit as CA
    CA.ACTION.set(CA.label_for(interaction))
    if perms.has(levels(svc, interaction), level):
        return True
    if level.startswith("FLAG:"):
        flag = level[5:]
        text = (f"{icons_lock()} This is **confidential alliance information**. It needs the `{flag}` permission, which an Admin "
                "can give to specific roles with `/bankset setaccess`. Being on the ECON team does not include it.")
    else:
        text = f"You need the **{level.title()}** permission for this command."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)
    return False


async def thinking(interaction: discord.Interaction, ephemeral: bool = True):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)


async def reply(interaction: discord.Interaction, content: str | None = None, card: A.Card | None = None,
                ephemeral: bool = True, file: discord.File | None = None, view=None):
    kw = {"ephemeral": ephemeral}
    if content:
        kw["content"] = content[:1900]
    if card:
        kw["embed"] = A.to_embed(card)
    if file:
        kw["file"] = file
    if view:
        kw["view"] = view
    if interaction.response.is_done():
        return await interaction.followup.send(**kw)
    await interaction.response.send_message(**kw)
    return await interaction.original_response()


def xlsx_file(data: bytes, name: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=name)


class ConfirmView(discord.ui.View):
    """Confirm / Cancel. Only the person who started it can press; works once."""

    def __init__(self, user_id: int, timeout: float):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.value: bool | None = None

    async def _guard(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This confirmation belongs to someone else.", ephemeral=True)
            return False
        if self.value is not None:
            await interaction.response.send_message("Already answered.", ephemeral=True)
            return False
        return True

    def _disable(self):
        for c in self.children:
            c.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.value = True
        self._disable()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.value = False
        self._disable()
        await interaction.response.edit_message(view=self)
        self.stop()


async def confirm(svc: Services, interaction: discord.Interaction, card: A.Card) -> bool:
    """Show the confirmation screen. True only if the user pressed Confirm in time."""
    from .config import cfg_int

    with svc.db.read() as conn:
        seconds = max(15, cfg_int(conn, "confirm_timeout_seconds"))
    card.add("Expires", f"This confirmation expires in {seconds} seconds.")
    view = ConfirmView(interaction.user.id, seconds)
    msg = await interaction.followup.send(embed=A.to_embed(card), view=view, ephemeral=True, wait=True)
    await view.wait()
    if view.value is None:
        view._disable()
        try:
            await msg.edit(content="Confirmation expired. Nothing was sent.", view=view)
        except discord.HTTPException:
            pass
        return False
    return bool(view.value)


async def post_outcomes(svc: Services, outcomes):
    """Send alerts for scanner outcomes (ECON log + member DMs)."""
    for o in outcomes:
        try:
            name = svc.alerts.member_name(o.nation_id) if o.nation_id else None
            if o.kind == "CREDIT":
                await svc.alerts.econ(A.deposit_card(o, name))
                if o.before is not None:
                    await svc.alerts.dm(svc.alerts.discord_id_for(o.nation_id), A.member_deposit_dm(o))
            elif o.kind in ("DONATION", "LOAN", "REVIEW", "OUTGOING_LINKED", "EXTERNAL_OUTFLOW", "OFFSHORE"):
                await svc.alerts.econ(A.classified_card(o, name))
        except Exception:  # noqa: BLE001
            log.exception("could not send alert for record %s", getattr(o, "record_id", "?"))
    try:
        await svc.alerts.flush_tax_turns(svc.prices)
    except Exception:  # noqa: BLE001
        log.exception("could not post the tax-turn summary")


async def _unused_post_events(svc: Services, findings):
    for f in findings:
        if f.get("is_new", True):
            await svc.alerts.econ(A.integrity_card(f))


async def nation_arg(svc: Services, interaction: discord.Interaction, text: str) -> int | None:
    """Resolve a typed nation (id / name / link / @user). On failure, tells the user and returns None."""
    try:
        return await resolve(svc, text)
    except ResolveError as exc:
        await reply(interaction, f"{exc}")
        return None


# ---------------------------------------------------------------- pagination
class PagedView(discord.ui.View):
    """Previous / Next buttons for long lists. Only the person who asked can use them."""

    def __init__(self, user_id: int, pages: list, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.user_id, self.pages, self.index = user_id, pages, 0
        self._label()

    def _label(self):
        for i, c in enumerate(self.pages):
            c.footer = f"Page {i + 1} of {len(self.pages)} · TUN Bank"

    def _sync_buttons(self):
        kids = list(self.children)
        if len(kids) >= 2:
            kids[0].disabled = self.index <= 0
            kids[1].disabled = self.index >= len(self.pages) - 1

    async def _go(self, interaction: discord.Interaction, step: int):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("These pages belong to someone else.", ephemeral=True)
        self.index = max(0, min(len(self.pages) - 1, self.index + step))
        self._sync_buttons()
        await interaction.response.edit_message(embed=A.to_embed(self.pages[self.index]), view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, disabled=True)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, -1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, +1)


async def paginate(interaction: discord.Interaction, pages: list):
    """Send one card, or several with Previous/Next buttons."""
    if len(pages) <= 1:
        return await reply(interaction, card=pages[0] if pages else A.Card("Nothing to show", "No records."))
    view = PagedView(interaction.user.id, pages)
    view._sync_buttons()
    return await reply(interaction, card=pages[0], view=view)


def chunk_cards(title: str, lines: list, *, per_page: int = 8, color: int = A.BLUE, empty: str = "Nothing here yet.",
                intro: str = "") -> list:
    """Split a long list of text lines into pages (cards)."""
    if not lines:
        return [A.Card(title, empty, color)]
    pages = []
    for i in range(0, len(lines), per_page):
        pages.append(A.Card(title, (intro + "\n\n" if intro else "") + "\n\n".join(lines[i:i + per_page]), color))
    return pages


class RefreshView(discord.ui.View):
    """A 🔄 Refresh button that rebuilds the same card with fresh data."""

    def __init__(self, user_id: int, builder, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.user_id, self.builder = user_id, builder

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This belongs to someone else.", ephemeral=True)
        await interaction.response.defer()
        try:
            card = await self.builder()
        except Exception:  # noqa: BLE001
            log.exception("refresh failed")
            return await interaction.followup.send("Could not refresh right now.", ephemeral=True)
        if card is not None:
            await interaction.edit_original_response(embed=A.to_embed(card), view=self)
