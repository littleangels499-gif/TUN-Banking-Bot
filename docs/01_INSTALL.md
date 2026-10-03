# 1. Install the tools (one time)

You need three free things: **Python**, **VS Code**, and **Git**. (You already use VS Code and GitHub — skip what you have.)

## Python
1. Go to https://www.python.org/downloads/ and download Python **3.12** (3.10 or newer works).
2. Run the installer. **Tick the box "Add python.exe to PATH"** on the first screen, then click *Install Now*.
3. Check it: open VS Code → menu **Terminal → New Terminal**, type `python --version` and press Enter.
   You should see `Python 3.12.x`. (On Mac/Linux you may need `python3`.)

## Open the project
1. Unzip the project folder somewhere simple (e.g. `Documents\tunbank_bot`).
2. VS Code → **File → Open Folder…** → choose the `tunbank_bot` folder.
3. If VS Code offers to install the **Python extension**, click *Install*.

## Install the bot's dependencies
In the VS Code terminal (Terminal → New Terminal), run these **one at a time**:

```
python -m venv .venv
```
```
.venv\Scripts\activate
```
(Mac/Linux: `source .venv/bin/activate`). Your prompt now starts with `(.venv)`.
```
pip install -r requirements.txt
```
Wait until it finishes. You only do this once (and again only if `requirements.txt` changes).

> If VS Code asks "Select Python interpreter", choose the one inside `.venv`.

**No database program is needed.** The bot uses SQLite, which is built into Python — the database is just one file.

Next: [2. Set up the .env file](02_SETUP_ENV.md)
