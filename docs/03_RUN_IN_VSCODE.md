# 3. Run the bot on your PC

1. Terminal → New Terminal. Make sure the prompt starts with `(.venv)` (if not: `.venv\Scripts\activate`).
2. Check setup: `python scripts/check_setup.py`
3. Start the bot: `python main.py`
   You should see `Logged in as TUN Bank#1234`. **Leave the window open** — closing it stops the bot.
4. Stop the bot: click in the terminal and press **Ctrl + C**.

Or use VS Code's **Run and Debug** panel (play-button icon on the left) → "Run TUN Bank" → green ▶.

The first start creates `data/tunbank.db` automatically. To run the safety tests: `python -m unittest discover -s tests`.

> **Important:** Do not run the same bot token on your PC and on Railway at the same time with *different* databases —
> you'd have two separate sets of books. While testing on your PC, use a separate test Discord application/token and a
> test alliance, or stop the Railway service first.

Next: [4. GitHub and Railway](04_GITHUB_AND_RAILWAY.md)
