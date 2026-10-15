# 19. Resource icons (the real in-game PnW icons)

By default the bot shows ordinary emoji (💵 🍞 ⚫ …). To show the **actual Politics & War resource icons**:

1. Put the 12 icons in the `icons/` folder as PNGs: `money.png food.png coal.png oil.png uranium.png iron.png bauxite.png lead.png gasoline.png munitions.png steel.png aluminum.png`
   (save them from the game; the artwork belongs to Politics & War, so it is not bundled).
   Shortcut: `python scripts/install_pnw_icons.py --download` tries to fetch any missing PNG from the game's website; if it cannot, it tells you which to save by hand.
2. Run `python scripts/install_pnw_icons.py` (needs `DISCORD_TOKEN` in your `.env`). It uploads them to the **bot application** as emojis named `pnw_money`, `pnw_food`, … Safe to re-run.
   Use `--dry-run` first to see what it would do.
3. In Discord: `/bankset iconpack` → *Apply the PnW icon pack*.

Why *application* emojis: they belong to the bot itself, so they work in **every server and in DMs**, with no server emoji slots used.

**Fallbacks.** A resource with no uploaded icon keeps its default emoji. If an uploaded icon is later deleted, the bot notices at start-up and switches that resource back to its default (it logs which). `/bankset iconpack` → *Go back to the default icons* undoes everything. Individual icons can still be set with `/bankset seticon`.
Charts and Excel files cannot draw custom emoji; they use the text name as before.
