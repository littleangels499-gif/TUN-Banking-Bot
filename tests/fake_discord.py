"""A tiny stand-in for discord.py so command logic can be tested without Discord or internet.

Only used by tests. The real bot uses the real discord.py (see requirements.txt).
"""
import asyncio
import sys
import types


def install():
    if "discord" in sys.modules and getattr(sys.modules["discord"], "_is_fake", False):
        return sys.modules["discord"]

    d = types.ModuleType("discord")
    d._is_fake = True

    class HTTPException(Exception):
        pass

    class Embed:
        def __init__(self, title=None, description=None, color=None):
            self.title, self.description, self.color = title, description, color
            self.fields = []

        def add_field(self, name, value, inline=False):
            self.fields.append((name, value, inline))

        def set_footer(self, text=""):
            self.footer_text = text

        def set_image(self, url=""):
            self.image_url = url

    class File:
        def __init__(self, fp, filename=None):
            self.fp, self.filename = fp, filename

    class Object:
        def __init__(self, id):
            self.id = id

    class Intents:
        @staticmethod
        def default():
            return Intents()

    class Attachment:
        def __init__(self, filename, data):
            self.filename, self._data, self.size = filename, data, len(data)

        async def read(self):
            return self._data

    class Role:
        def __init__(self, id):
            self.id, self.mention = id, f"<@&{id}>"

    class User:
        def __init__(self, id, name="user"):
            self.id, self.name, self.mention = id, name, f"<@{id}>"
            self.roles = []

        def __str__(self):
            return self.name

    class TextChannel:
        def __init__(self, id):
            self.id, self.mention = id, f"<#{id}>"

    class ButtonStyle:
        success = danger = primary = secondary = 1

    class _Button:
        def __init__(self, label=None, emoji=None, style=None, disabled=False, custom_id=None, **kw):
            self.label, self.emoji, self.style, self.disabled, self.custom_id = label, emoji, style, disabled, custom_id
            self.callback = None

    class SelectOption:
        def __init__(self, label="", value=None, **kw):
            self.label, self.value = label, value

    class Select:
        def __init__(self, placeholder="", options=None, min_values=1, max_values=1, **kw):
            self.placeholder, self.options, self.values, self.callback, self.disabled = placeholder, list(options or []), [], None, False

    class TextStyle:
        short = 1
        paragraph = 2

    class TextInput:
        def __init__(self, label="", placeholder="", required=True, max_length=None, style=None, **kw):
            self.label, self.placeholder, self.required, self.value = label, placeholder, required, ""

    class Modal:
        def __init__(self, title=""):
            self.title, self.children = title, []

        def add_item(self, item):
            self.children.append(item)

    class View:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.children = [types.SimpleNamespace(disabled=False), types.SimpleNamespace(disabled=False)]
            self._evt = asyncio.Event()

        def add_item(self, item):
            self.children.append(item)

        def clear_items(self):
            self.children = []

        def stop(self):
            self._evt.set()

        async def wait(self):
            try:
                await asyncio.wait_for(self._evt.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    def button(**kw):
        def deco(fn):
            return fn
        return deco

    ui = types.ModuleType("discord.ui")
    ui.View, ui.Button, ui.button, ui.Modal, ui.TextInput, ui.Select = View, _Button, button, Modal, TextInput, Select

    class Interaction:
        pass

    # ---- app_commands
    ac = types.ModuleType("discord.app_commands")

    class AppCommandError(Exception):
        pass

    class Command:
        def __init__(self, callback, name, description=""):
            self.callback, self.name, self.description = callback, name, description
            self.autocompletes = {}

        def autocomplete(self, param):
            import inspect
            if param not in inspect.signature(self.callback).parameters:
                raise TypeError(f"/{self.name} has no parameter '{param}' to autocomplete")
            def deco(fn):
                self.autocompletes[param] = fn
                return fn
            return deco

    class Group:
        def __init__(self, name, description=""):
            import re
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
                raise ValueError(f"bad group name {name}")
            self.name, self.commands = name, {}

        def command(self, name=None, description=""):
            def deco(fn):
                n = name or fn.__name__
                if len(self.commands) >= 25:
                    raise ValueError("maximum number of child commands exceeded")
                import inspect
                if len(inspect.signature(fn).parameters) - 1 > 25:
                    raise ValueError("too many parameters")
                if n in self.commands:
                    raise ValueError(f"duplicate command /{self.name} {n}")
                if n != n.lower() or len(n) > 32:
                    raise ValueError(f"bad command name {n}")
                if len(description) > 100:
                    raise ValueError(f"description too long for /{self.name} {n}: {len(description)}")
                self.commands[n] = Command(fn, n, description)
                return self.commands[n]
            return deco

    class _ChoiceMeta(type):
        def __getitem__(cls, item):
            return cls

    class Choice(metaclass=_ChoiceMeta):
        def __init__(self, name, value):
            self.name, self.value = name, value

    def describe(**kw):
        return lambda fn: fn

    def choices(**kw):
        return lambda fn: fn

    ac.Group, ac.Choice, ac.describe, ac.choices, ac.AppCommandError = Group, Choice, describe, choices, AppCommandError
    ac.Command = Command
    ac.Attachment = Attachment

    # ---- ext
    ext = types.ModuleType("discord.ext")
    commands = types.ModuleType("discord.ext.commands")
    tasks = types.ModuleType("discord.ext.tasks")

    class Tree:
        def __init__(self):
            self.cmds = {}

        def command(self, name=None, description=""):
            def deco(fn):
                self.top = getattr(self, "top", {})
                cmd = Command(fn, name or fn.__name__, description)
                self.top[cmd.name] = cmd
                return cmd
            return deco

        def get_commands(self):
            return list(self.cmds.values()) + list(getattr(self, "top", {}).values())

        def add_command(self, g):
            if g.name in self.cmds:
                raise ValueError("duplicate group")
            self.cmds[g.name] = g

        def copy_global_to(self, guild):
            pass

        async def sync(self, guild=None):
            pass

    class Bot:
        def __init__(self, command_prefix=None, intents=None):
            self.tree = Tree()
            self.user = "bot"
            self.views = []

        def add_view(self, view):
            self.views.append(view)

        def get_channel(self, i):
            return None

    commands.Bot = Bot
    commands.when_mentioned = object()

    class _Loop:
        def __init__(self, fn):
            self.fn = fn

        def before_loop(self, f):
            return f

        def start(self):
            pass

        def cancel(self):
            pass

        def __get__(self, obj, cls):
            return self

    def loop(**kw):
        return lambda fn: _Loop(fn)

    tasks.loop = loop
    ext.commands, ext.tasks = commands, tasks

    d.HTTPException, d.Embed, d.File, d.Object, d.Intents = HTTPException, Embed, File, Object, Intents
    d.Attachment, d.Role, d.User, d.TextChannel, d.ButtonStyle = Attachment, Role, User, TextChannel, ButtonStyle
    d.Member = User
    d.Interaction = Interaction
    d.TextStyle = TextStyle
    d.SelectOption = SelectOption
    d.ui, d.app_commands, d.ext = ui, ac, ext
    sys.modules.update({"discord": d, "discord.ui": ui, "discord.app_commands": ac, "discord.ext": ext,
                        "discord.ext.commands": commands, "discord.ext.tasks": tasks})
    return d


class FakeMessage:
    async def edit(self, **kw):
        pass


class FakeInteraction:
    _next = 1000

    def __init__(self, user, auto_confirm=True, press=True):
        FakeInteraction._next += 1
        self.id = FakeInteraction._next
        self.user = user
        self.sent = []
        self.auto_confirm = auto_confirm
        self.press = press
        self.response = self._Resp(self)
        self.followup = self._Follow(self)

    class _Resp:
        def __init__(self, i):
            self.i, self._done = i, False

        def is_done(self):
            return self._done

        async def defer(self, **kw):
            self._done = True

        async def send_message(self, content=None, **kw):
            self._done = True
            self.i.sent.append({"content": content, **kw})

        async def edit_message(self, **kw):
            self.i.edited = kw

        async def send_modal(self, modal):
            self._done = True
            self.i.modal = modal

    class _Follow:
        def __init__(self, i):
            self.i = i

        async def send(self, content=None, **kw):
            self.i.sent.append({"content": content, **kw})
            view = kw.get("view")
            if view is not None and self.i.press and hasattr(view, 'confirm'):
                async def press():
                    await asyncio.sleep(0)
                    if self.i.auto_confirm:
                        await view.confirm(self.i, None)
                    else:
                        await view.cancel(self.i, None)
                asyncio.ensure_future(press())
            return FakeMessage()

    async def original_response(self):
        return FakeMessage()

    async def edit_original_response(self, **kw):
        self.edited = kw

    # --- test helpers: press a button / submit a form
    def view(self):
        for m in reversed(self.sent):
            if m.get("view") is not None:
                return m["view"]
        return None

    # helpers for assertions
    def text(self):
        out = []
        for m in self.sent:
            if m.get("content"):
                out.append(m["content"])
            e = m.get("embed")
            if e:
                out.append(f"{e.title} {e.description}")
                out += [f"{n}: {v}" for n, v, _ in e.fields]
        return "\n".join(out)
