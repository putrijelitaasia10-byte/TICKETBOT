#!/usr/bin/env python3
"""
Discord ticket bot.

Members open a private ticket channel by picking a category from a persistent
panel. Support roles can see and answer tickets, claim them, add other members,
and close them with a transcript that is posted to a log channel.

Configuration lives in data/tickets.json and is managed with the /ticket
slash commands. Slash commands, buttons, modals and a JSON file are the only
moving parts -- no database and no web server.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DATA_FILE = BASE_DIR / "data" / "tickets.json"

log = logging.getLogger("tickets")

MAX_TRANSCRIPT_MESSAGES = 1500
MAX_TYPES = 25
PANEL_FOOTER = "Ticket system • pick a category below"
CLOSE_DELAY_SECONDS = 5

DEFAULT_TYPES = (
    ("support", "General Support", "Get help with anything", "🛠️"),
    ("report", "Report a Member", "Report rule breaking or abuse", "🚨"),
    ("other", "Something Else", "Anything that does not fit above", "❓"),
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_list(values: object) -> list[int]:
    """Coerce a JSON list of IDs into clean ints, dropping anything invalid."""
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[int] = []
    for value in values:
        parsed = _int_or_none(value)
        if parsed is not None:
            result.append(parsed)
    return result


def slugify(text: str, fallback: str = "ticket") -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    text = re.sub(r"-{2,}", "-", text)
    return (text or fallback)[:80]


def channel_name(text: str, fallback: str = "ticket") -> str:
    """Discord channel names: lowercase, no spaces, max 100 characters."""
    return slugify(text, fallback)[:95]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def relative_time(value: str) -> str:
    parsed = parse_iso(value)
    if parsed is None:
        return "unknown"
    return f"<t:{int(parsed.timestamp())}:R>"


def format_duration(started: str, ended: datetime | None = None) -> str:
    parsed = parse_iso(started)
    if parsed is None:
        return "unknown"
    ended = ended or datetime.now(timezone.utc)
    seconds = max(0, int((ended - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def yes_no(value: bool) -> str:
    return "✅ enabled" if value else "❌ disabled"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class TicketType:
    """One selectable category on the ticket panel."""

    key: str = "support"
    label: str = "General Support"
    description: str = ""
    emoji: str | None = None
    category_id: int | None = None
    ping_role_ids: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.key = slugify(str(self.key), "ticket")
        self.label = truncate(str(self.label), 80) or "Ticket"
        self.description = truncate(str(self.description or ""), 100)
        if self.emoji is not None:
            self.emoji = str(self.emoji).strip() or None
        self.category_id = _int_or_none(self.category_id)
        self.ping_role_ids = _int_list(self.ping_role_ids)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> TicketType:
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class GuildConfig:
    """Per-guild ticket configuration."""

    enabled: bool = True
    category_id: int | None = None
    log_channel_id: int | None = None
    panel_channel_id: int | None = None
    panel_message_id: int | None = None
    support_role_ids: list[int] = field(default_factory=list)
    types: list[TicketType] = field(default_factory=list)
    max_open_per_user: int = 1
    counter: int = 0
    total_opened: int = 0

    def __post_init__(self) -> None:
        self.category_id = _int_or_none(self.category_id)
        self.log_channel_id = _int_or_none(self.log_channel_id)
        self.panel_channel_id = _int_or_none(self.panel_channel_id)
        self.panel_message_id = _int_or_none(self.panel_message_id)
        self.support_role_ids = _int_list(self.support_role_ids)
        try:
            self.max_open_per_user = max(1, min(int(self.max_open_per_user), 25))
        except (TypeError, ValueError):
            self.max_open_per_user = 1
        try:
            self.counter = max(0, int(self.counter))
            self.total_opened = max(0, int(self.total_opened))
        except (TypeError, ValueError):
            self.counter = 0
            self.total_opened = 0

        cleaned: list[TicketType] = []
        for entry in self.types or []:
            if isinstance(entry, TicketType):
                cleaned.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            try:
                cleaned.append(TicketType.from_dict(entry))
            except (TypeError, ValueError):
                log.warning("Skipping malformed ticket type: %r", entry)
        self.types = cleaned[:MAX_TYPES]
        if not self.types:
            self.types = [
                TicketType(key=key, label=label, description=desc, emoji=emoji)
                for key, label, desc, emoji in DEFAULT_TYPES
            ]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> GuildConfig:
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def find_type(self, key: str) -> TicketType | None:
        for entry in self.types:
            if entry.key == key:
                return entry
        return None


@dataclass
class Ticket:
    """An open ticket. The channel ID doubles as the primary key."""

    channel_id: int
    guild_id: int
    number: int
    type_key: str
    owner_id: int
    created_at: str
    claimed_by: int | None = None
    status: str = "open"
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Ticket:
        known = {f.name for f in fields(cls)}
        ticket = cls(**{key: value for key, value in data.items() if key in known})
        ticket.channel_id = int(ticket.channel_id)
        ticket.guild_id = int(ticket.guild_id)
        ticket.owner_id = int(ticket.owner_id)
        ticket.number = int(ticket.number)
        ticket.claimed_by = _int_or_none(ticket.claimed_by)
        ticket.reason = str(ticket.reason or "")[:2000]
        return ticket


class TicketStore:
    """JSON-backed store for guild configuration and open tickets."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._guilds: dict[int, GuildConfig] = {}
        self._tickets: dict[int, Ticket] = {}
        self._lock = asyncio.Lock()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("store root must be a JSON object")

            guilds = raw.get("guilds", {})
            if not isinstance(guilds, dict):
                raise ValueError("'guilds' must be a JSON object")
            for guild_id, data in guilds.items():
                if not isinstance(data, dict):
                    continue
                try:
                    self._guilds[int(guild_id)] = GuildConfig.from_dict(data)
                except (TypeError, ValueError) as exc:
                    log.warning("Skipping broken config for guild %s: %s", guild_id, exc)

            tickets = raw.get("tickets", {})
            if not isinstance(tickets, dict):
                raise ValueError("'tickets' must be a JSON object")
            for channel_id, data in tickets.items():
                if not isinstance(data, dict):
                    continue
                try:
                    self._tickets[int(channel_id)] = Ticket.from_dict(data)
                except (TypeError, ValueError) as exc:
                    log.warning("Skipping broken ticket %s: %s", channel_id, exc)
        except (OSError, ValueError) as exc:
            broken = self.path.with_suffix(".corrupt.json")
            log.error("Could not read %s (%s). Moving it to %s.", self.path, exc, broken.name)
            with contextlib.suppress(OSError):
                os.replace(self.path, broken)

    def peek_config(self, guild_id: int) -> GuildConfig | None:
        return self._guilds.get(guild_id)

    def config(self, guild_id: int) -> GuildConfig:
        config = self._guilds.get(guild_id)
        if config is None:
            config = GuildConfig()
            self._guilds[guild_id] = config
        return config

    def reset(self, guild_id: int) -> None:
        self._guilds[guild_id] = GuildConfig()
        for channel_id, ticket in list(self._tickets.items()):
            if ticket.guild_id == guild_id:
                del self._tickets[channel_id]

    # -- tickets ------------------------------------------------------------ #

    def ticket(self, channel_id: int) -> Ticket | None:
        return self._tickets.get(channel_id)

    def add_ticket(self, ticket: Ticket) -> None:
        self._tickets[ticket.channel_id] = ticket

    def remove_ticket(self, channel_id: int) -> Ticket | None:
        return self._tickets.pop(channel_id, None)

    def tickets_for_guild(self, guild_id: int) -> list[Ticket]:
        return [ticket for ticket in self._tickets.values() if ticket.guild_id == guild_id]

    def open_tickets_for_user(self, guild_id: int, user_id: int) -> list[Ticket]:
        return [
            ticket
            for ticket in self._tickets.values()
            if ticket.guild_id == guild_id and ticket.owner_id == user_id
        ]

    # -- persistence -------------------------------------------------------- #

    async def save(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "guilds": {str(gid): config.to_dict() for gid, config in self._guilds.items()},
            "tickets": {str(cid): ticket.to_dict() for cid, ticket in self._tickets.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #


def load_env_file(path: Path) -> None:
    """Minimal .env loader so the bot has no dependency beyond discord.py."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_ids(name: str) -> list[int]:
    ids: list[int] = []
    for chunk in os.getenv(name, "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ids.append(int(chunk))
        elif chunk:
            log.warning("Ignoring %r in %s: not a numeric ID.", chunk, name)
    return ids


# --------------------------------------------------------------------------- #
# Transcripts
# --------------------------------------------------------------------------- #


def render_transcript(
    guild_name: str,
    channel_name: str,
    ticket: Ticket,
    type_label: str,
    messages: list[discord.Message],
    note: str | None = None,
) -> bytes:
    """Render a self-contained HTML transcript."""
    created = parse_iso(ticket.created_at)
    created_text = created.strftime("%Y-%m-%d %H:%M UTC") if created else ticket.created_at

    parts: list[str] = []
    for message in messages:
        author = html.escape(str(message.author))
        stamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = html.escape(message.content or "")
        content = content.replace("\n", "<br>")
        if not content:
            content = "<em class='muted'>(no text)</em>"

        attachments = ""
        if message.attachments:
            links = "".join(
                f"<li><a href='{html.escape(a.url)}'>{html.escape(a.filename)}</a></li>"
                for a in message.attachments
            )
            attachments = f"<ul class='attachments'>{links}</ul>"

        embeds = ""
        if message.embeds:
            embeds = f"<div class='muted'>[{len(message.embeds)} embed(s)]</div>"

        parts.append(
            "<div class='msg'>"
            f"<div class='meta'><span class='author'>{author}</span>"
            f"<span class='time'>{stamp}</span></div>"
            f"<div class='content'>{content}</div>{attachments}{embeds}</div>"
        )

    body = "\n".join(parts) if parts else "<p class='muted'>No messages were recorded.</p>"
    note_html = f"<p class='note'>{html.escape(note)}</p>" if note else ""

    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ticket #{ticket.number:04d} - {html.escape(guild_name)}</title>
<style>
  body {{ background:#313338; color:#dbdee1; font-family:"Segoe UI",Arial,sans-serif;
         margin:0; padding:24px; }}
  header {{ border-bottom:1px solid #4f545c; margin-bottom:20px; padding-bottom:16px; }}
  h1 {{ color:#f2f3f5; font-size:22px; margin:0 0 8px; }}
  .muted {{ color:#949ba4; }}
  .msg {{ background:#2b2d31; border-radius:8px; margin:0 0 12px; padding:10px 14px; }}
  .meta {{ display:flex; gap:12px; align-items:baseline; margin-bottom:4px; }}
  .author {{ color:#f2f3f5; font-weight:600; }}
  .time {{ color:#949ba4; font-size:12px; }}
  .content {{ line-height:1.45; word-wrap:break-word; }}
  .attachments {{ margin:8px 0 0; padding-left:20px; }}
  a {{ color:#00a8fc; }}
  .note {{ background:#3b2f2f; border-left:4px solid #f0b232; padding:10px 14px; }}
  footer {{ color:#949ba4; font-size:12px; margin-top:24px; }}
</style>
</head>
<body>
<header>
  <h1>Ticket #{ticket.number:04d} — {html.escape(type_label)}</h1>
  <div class="muted">Server: {html.escape(guild_name)} • Channel: #{html.escape(channel_name)}</div>
  <div class="muted">Opened: {html.escape(created_text)} • Messages: {len(messages)}</div>
</header>
{note_html}
<main>
{body}
</main>
<footer>Generated by the ticket bot.</footer>
</body>
</html>
"""
    return document.encode("utf-8")


# --------------------------------------------------------------------------- #
# Views and modals
# --------------------------------------------------------------------------- #


class CloseModal(discord.ui.Modal, title="Close this ticket"):
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
        placeholder="Why is this ticket being closed? (optional)",
    )

    def __init__(self, bot: TicketBot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.close_from_interaction(interaction, str(self.reason.value or "").strip())


class AddUserModal(discord.ui.Modal, title="Add member to ticket"):
    user_input: discord.ui.TextInput = discord.ui.TextInput(
        label="User ID or @mention",
        max_length=100,
        placeholder="123456789012345678",
    )

    def __init__(self, bot: TicketBot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.add_member_to_ticket(interaction, str(self.user_input.value or "").strip())


class TicketOpenModal(discord.ui.Modal):
    """Asks the member what their issue is before opening a ticket."""

    def __init__(self, bot: TicketBot, type_key: str) -> None:
        super().__init__(title="Open a ticket", timeout=600)
        self.bot = bot
        self.type_key = type_key
        self.issue: discord.ui.TextInput = discord.ui.TextInput(
            label="What is the issue?",
            style=discord.TextStyle.paragraph,
            placeholder="Describe your problem in a sentence or two…",
            max_length=1000,
            required=True,
        )
        self.add_item(self.issue)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.issue.value or "").strip()
        if not reason:
            await self.bot.reply_error(interaction, "Please describe your issue before opening the ticket.")
            return
        await self.bot.open_from_panel(interaction, self.type_key, reason)


class TicketPanelView(discord.ui.View):
    """Persistent select menu used to open tickets."""

    def __init__(self, bot: TicketBot, types: list[TicketType] | None = None) -> None:
        super().__init__(timeout=None)
        self.bot = bot

        options = [
            discord.SelectOption(
                label=truncate(ttype.label, 100),
                value=ttype.key,
                description=truncate(ttype.description, 100) or None,
                emoji=ttype.emoji or None,
            )
            for ttype in (types or [TicketType()])[:MAX_TYPES]
        ]
        select = discord.ui.Select(
            custom_id="ticket:open",
            placeholder="Open a ticket…",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self._open
        self.add_item(select)

    async def _open(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        assert isinstance(select, discord.ui.Select)
        type_key = select.values[0]

        guild = interaction.guild
        if guild is not None and isinstance(interaction.user, discord.Member):
            config = self.bot.store.config(guild.id)
            if config.find_type(type_key) is None:
                await self.bot.reply_error(
                    interaction,
                    "That ticket category no longer exists. Ask a moderator to refresh the panel.",
                    title="Category unavailable",
                )
                return
            existing = self.bot.store.open_tickets_for_user(guild.id, interaction.user.id)
            if len(existing) >= config.max_open_per_user:
                channel = guild.get_channel(existing[0].channel_id)
                where = channel.mention if channel else "your existing ticket"
                await self.bot.reply_error(
                    interaction,
                    f"You already have an open ticket: {where}\n"
                    "Please use it, or close it before opening another one.",
                    title="Ticket already open",
                )
                return

        await interaction.response.send_modal(TicketOpenModal(self.bot, type_key))

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        await self.bot.report_view_error(interaction, error)


class TicketControlView(discord.ui.View):
    """Persistent buttons attached to every ticket channel."""

    def __init__(self, bot: TicketBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Claim",
        emoji="🙋",
        style=discord.ButtonStyle.primary,
        custom_id="ticket:claim",
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.bot.claim_from_button(interaction)

    @discord.ui.button(
        label="Add member",
        emoji="➕",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket:adduser",
    )
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None:
            await self.bot.reply_error(interaction, "Tickets only work inside a server.")
            return
        config = self.bot.store.config(interaction.guild.id)
        if not self.bot.is_staff(interaction.user, config):
            await self.bot.reply_error(interaction, "Only support staff can add members to a ticket.")
            return
        await interaction.response.send_modal(AddUserModal(self.bot))

    @discord.ui.button(
        label="Close",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:close",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or interaction.channel_id is None:
            await self.bot.reply_error(interaction, "Tickets only work inside a server.")
            return
        ticket = self.bot.store.ticket(interaction.channel_id)
        if ticket is None:
            await self.bot.reply_error(interaction, "This channel is not a tracked ticket.")
            return
        config = self.bot.store.config(interaction.guild.id)
        if not self.bot.can_close(interaction.user, ticket, config):
            await self.bot.reply_error(
                interaction,
                "Only the ticket owner or support staff can close this ticket.",
            )
            return
        await interaction.response.send_modal(CloseModal(self.bot))

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        await self.bot.report_view_error(interaction, error)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #


class TicketError(Exception):
    """A user-facing problem that prevents an action."""


class TicketBot(commands.Bot):
    def __init__(self, store: TicketStore) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.store = store
        self._creating: set[tuple[int, int]] = set()
        self._create_locks: dict[int, asyncio.Lock] = {}
        self._closing: set[int] = set()
        # IMPORTANT: these are created in setup_hook(), not here. discord.py
        # marks a View created without a running event loop as already stopped,
        # which silently discards every button/select interaction.
        self.panel_view: TicketPanelView
        self.control_view: TicketControlView
        self.tickets_opened = 0
        self.tickets_closed = 0

    # -- lifecycle ---------------------------------------------------------- #

    async def setup_hook(self) -> None:
        self.panel_view = TicketPanelView(self)
        self.control_view = TicketControlView(self)
        await self.add_cog(TicketCog(self))
        self.add_view(self.panel_view)
        self.add_view(self.control_view)
        log.info(
            "Persistent components ready: %s",
            sorted(item.custom_id for view in (self.panel_view, self.control_view) for item in view.children),
        )
        await self._sync_commands()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        # Log every component click so a silently discarded interaction is
        # obvious in logs/ticket-bot.log.
        if interaction.type is discord.InteractionType.component:
            data = interaction.data or {}
            log.info(
                "Component interaction %r by %s (%s) in channel %s",
                data.get("custom_id"),
                interaction.user,
                interaction.user.id,
                interaction.channel_id,
            )

    async def _sync_commands(self) -> None:
        guild_id = os.getenv("GUILD_ID", "").strip()
        if not guild_id:
            try:
                synced = await self.tree.sync()
                log.info(
                    "Synced %d global commands. They can take up to an hour to appear; "
                    "set GUILD_ID in .env for instant updates.",
                    len(synced),
                )
            except discord.HTTPException as exc:
                log.error("Could not sync global slash commands: %s", exc)
            return

        try:
            guild = discord.Object(id=int(guild_id))
        except ValueError:
            log.error("GUILD_ID=%r is not a numeric server ID; using global commands instead.", guild_id)
            try:
                await self.tree.sync()
            except discord.HTTPException as exc:
                log.error("Could not sync global slash commands: %s", exc)
            return

        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to guild %s (immediate).", len(synced), guild_id)
        except discord.HTTPException as exc:
            log.error("Could not sync commands to guild %s: %s", guild_id, exc)
            try:
                synced = await self.tree.sync()
                log.info("Fell back to %d global commands.", len(synced))
            except discord.HTTPException as fallback_exc:
                log.error("Global command sync also failed: %s", fallback_exc)
            return

        # Keep a single copy of every command: the guild-scoped one.
        try:
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            log.info("Cleared global command copies; guild commands remain active.")
        except discord.HTTPException as exc:
            log.warning("Could not clear the global command copies: %s", exc)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s), serving %d guild(s).", self.user, self.user.id, len(self.guilds))
        if not self.guilds:
            log.error(
                "I am not in any server! Invite me with: "
                "https://discord.com/oauth2/authorize?client_id=%s&scope=bot%%20applications.commands"
                "&permissions=268561424",
                self.user.id,
            )
            return
        seeded = False
        for guild in self.guilds:
            if self.store.peek_config(guild.id) is None:
                self._seed_guild(guild)
                seeded = True
        if seeded:
            await self.store.save()
        pruned = self._warn_about_misconfiguration()
        if pruned and not seeded:
            await self.store.save()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        self._seed_guild(guild)
        await self.store.save()

    def _seed_guild(self, guild: discord.Guild) -> None:
        if self.store.peek_config(guild.id) is not None:
            return
        config = self.store.config(guild.id)
        category_id = _int_or_none(os.getenv("TICKET_CATEGORY_ID", "").strip())
        if category_id and guild.get_channel(category_id) is not None:
            config.category_id = category_id
        log_channel = _int_or_none(os.getenv("TICKET_LOG_CHANNEL_ID", "").strip())
        if log_channel and guild.get_channel(log_channel) is not None:
            config.log_channel_id = log_channel
        for role_id in env_ids("TICKET_SUPPORT_ROLE_IDS"):
            if guild.get_role(role_id) is not None:
                config.support_role_ids.append(role_id)
        log.info("[%s] Created ticket settings (types=%d).", guild.name, len(config.types))

    def _warn_about_misconfiguration(self) -> bool:
        pruned = False
        for guild in self.guilds:
            config = self.store.peek_config(guild.id)
            me = guild.me
            if config is None or me is None:
                continue
            if not me.guild_permissions.manage_channels:
                log.warning("[%s] Missing permission: Manage Channels (required to create tickets).", guild.name)
            if config.category_id and guild.get_channel(config.category_id) is None:
                log.warning("[%s] Configured ticket category no longer exists.", guild.name)
            if config.log_channel_id and guild.get_channel(config.log_channel_id) is None:
                log.warning("[%s] Configured transcript log channel no longer exists.", guild.name)
            if config.panel_channel_id and guild.get_channel(config.panel_channel_id) is None:
                log.warning("[%s] Configured ticket panel channel no longer exists.", guild.name)
            for ticket in self.store.tickets_for_guild(guild.id):
                if guild.get_channel(ticket.channel_id) is None:
                    self.store.remove_ticket(ticket.channel_id)
                    pruned = True
                    log.info("[%s] Pruned ticket #%04d (channel deleted).", guild.name, ticket.number)
        return pruned

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        ticket = self.store.remove_ticket(channel.id)
        if ticket is not None:
            await self.store.save()
            log.info("Ticket #%04d was deleted outside the bot.", ticket.number)

    # -- permission helpers ------------------------------------------------- #

    @staticmethod
    def is_manager(member: discord.Member) -> bool:
        permissions = member.guild_permissions
        return permissions.administrator or permissions.manage_guild

    def is_staff(self, member: discord.Member, config: GuildConfig) -> bool:
        if self.is_manager(member):
            return True
        if member.guild_permissions.manage_channels:
            return True
        support = set(config.support_role_ids)
        return bool(support) and any(role.id in support for role in member.roles)

    def can_close(self, member: discord.Member, ticket: Ticket, config: GuildConfig) -> bool:
        return member.id == ticket.owner_id or self.is_staff(member, config)

    # -- interaction helpers ------------------------------------------------ #

    async def reply_error(
        self, interaction: discord.Interaction, message: str, title: str = "Not allowed"
    ) -> None:
        embed = discord.Embed(title=title, description=message, color=0xED4245)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.debug("Could not send error reply", exc_info=True)

    async def report_view_error(self, interaction: discord.Interaction, error: Exception) -> None:
        if isinstance(error, TicketError):
            await self.reply_error(interaction, str(error))
            return
        if isinstance(error, discord.Forbidden):
            await self.reply_error(
                interaction,
                "Discord denied the action. Give my role **Manage Channels**, "
                "**Manage Roles** and **Manage Messages**.",
                title="Missing permissions",
            )
            return
        log.exception("Ticket component failed", exc_info=error)
        await self.reply_error(
            interaction,
            "Something went wrong. Check the bot console for details.",
            title="Command failed",
        )

    # -- panel -------------------------------------------------------------- #

    def panel_embed(self, guild: discord.Guild, config: GuildConfig) -> discord.Embed:
        embed = discord.Embed(
            title="🎫 Support Tickets",
            description=(
                "Need help, want to report something, or have a question?\n"
                "Pick a category below, describe your issue, and a **private channel** "
                "will be created for you.\n\n"
                "Only you and the support team can see your ticket."
            ),
            color=0x5865F2,
        )
        embed.add_field(
            name="How it works",
            value=(
                "1. Choose a category.\n"
                "2. Type what you need help with in the popup.\n"
                "3. Wait for a staff member to reply.\n"
                "4. Close the ticket with the 🔒 button when you are done."
            ),
            inline=False,
        )
        if config.support_role_ids:
            roles = [f"<@&{rid}>" for rid in config.support_role_ids[:10]]
            embed.add_field(name="Support team", value=" ".join(roles), inline=False)
        embed.set_footer(text=PANEL_FOOTER)
        return embed

    async def post_panel(self, channel: discord.abc.GuildChannel, config: GuildConfig) -> discord.Message:
        if not isinstance(channel, discord.TextChannel):
            raise TicketError("The panel must be posted in a normal text channel.")
        if not config.types:
            raise TicketError("There are no ticket categories configured.")

        # Remove a previously posted panel so the channel only has one.
        if config.panel_message_id and config.panel_channel_id:
            old_channel = channel.guild.get_channel(config.panel_channel_id)
            if isinstance(old_channel, discord.TextChannel):
                with contextlib.suppress(discord.HTTPException):
                    old_message = await old_channel.fetch_message(config.panel_message_id)
                    await old_message.delete()

        view = TicketPanelView(self, config.types)
        message = await channel.send(embed=self.panel_embed(channel.guild, config), view=view)
        config.panel_channel_id = channel.id
        config.panel_message_id = message.id
        await self.store.save()
        return message

    # -- opening tickets ---------------------------------------------------- #

    async def open_from_panel(
        self, interaction: discord.Interaction, type_key: str, reason: str = ""
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await self.reply_error(interaction, "Tickets can only be opened inside a server.")
            return

        config = self.store.config(guild.id)
        if not config.enabled:
            await self.reply_error(interaction, "The ticket system is currently disabled.")
            return

        ttype = config.find_type(type_key)
        if ttype is None:
            await self.reply_error(
                interaction,
                "That ticket category no longer exists. Ask a moderator to refresh the panel.",
                title="Category unavailable",
            )
            return

        key = (guild.id, interaction.user.id)
        if key in self._creating:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Slow down",
                    description="Your ticket is already being created…",
                    color=0xFEE75C,
                ),
                ephemeral=True,
            )
            return

        self._creating.add(key)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            channel = await self.create_ticket(guild, interaction.user, ttype, config, reason)
        except TicketError as exc:
            log.warning("[%s] Ticket not opened for %s: %s", guild.name, interaction.user, exc)
            await interaction.followup.send(
                embed=discord.Embed(title="Could not open a ticket", description=str(exc), color=0xED4245),
                ephemeral=True,
            )
            return
        except discord.Forbidden as exc:
            log.warning(
                "[%s] Channel creation was forbidden: %s (code %s)",
                guild.name,
                exc.text,
                exc.code,
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Could not open a ticket",
                    description=(
                        f"Discord said: **{exc.text or exc}** (code {exc.code})\n\n"
                        "Run `/ticket diagnose` for the exact fix. The usual causes:\n"
                        "• My role is missing **Manage Channels**.\n"
                        "• My role is not allowed **View Channel** + **Manage Channels** "
                        "in the ticket category.\n"
                        "• The category has an overwrite that denies me access.\n\n"
                        "A bot can only grant permissions in a new ticket channel that "
                        "it already has in the parent category."
                    ),
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Could not open a ticket",
                    description=f"Discord returned an error: {exc.text or exc}",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return
        finally:
            self._creating.discard(key)

        await interaction.followup.send(
            embed=discord.Embed(
                title="Ticket opened",
                description=f"Your ticket is ready: {channel.mention}",
                color=0x57F287,
            ),
            ephemeral=True,
        )

    async def create_ticket(
        self,
        guild: discord.Guild,
        author: discord.Member,
        ttype: TicketType,
        config: GuildConfig,
        reason: str = "",
    ) -> discord.TextChannel:
        lock = self._create_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            existing = self.store.open_tickets_for_user(guild.id, author.id)
            if len(existing) >= config.max_open_per_user:
                channel = guild.get_channel(existing[0].channel_id)
                where = channel.mention if channel else "your existing ticket"
                raise TicketError(
                    f"You already have an open ticket: {where}\n"
                    "Please use it, or close it before opening another one."
                )

            category = self._resolve_category(guild, ttype, config)
            number = config.counter + 1
            name = channel_name(f"{ttype.key}-{number:04d}")

            overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                author: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                ),
            }

            me = guild.me
            if me is not None:
                # NOTE: never include manage_permissions/Manage Roles here.
                # Discord only allows MANAGE_ROLES in a channel overwrite when
                # creating the channel if the bot is an Administrator; otherwise
                # it returns 403 "Missing Permissions" (50013). The bot already
                # has Manage Roles at guild level, which is enough to add members.
                overwrites[me] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    manage_channels=True,
                    manage_messages=True,
                )

            support_roles = [
                guild.get_role(role_id)
                for role_id in dict.fromkeys(config.support_role_ids + ttype.ping_role_ids)
            ]
            staff_overwrite = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                manage_messages=True,
            )
            for role in support_roles:
                if role is not None and role != guild.default_role:
                    overwrites[role] = staff_overwrite

            topic = truncate(
                f"Ticket #{number:04d} • {ttype.label} • opened by {author} ({author.id})", 1024
            )
            try:
                channel = await guild.create_text_channel(
                    name=name,
                    category=category,
                    overwrites=overwrites,
                    topic=topic,
                    reason=f"Ticket #{number:04d} opened by {author} ({author.id})",
                )
            except discord.Forbidden as exc:
                if exc.code != 50013 or me is None:
                    log.warning(
                        "[%s] create_text_channel forbidden for %s: %s (code %s)",
                        guild.name,
                        author,
                        exc.text,
                        exc.code,
                    )
                    raise
                # Some guilds reject extra permissions in a channel overwrite
                # (Discord only lets Administrators set Manage Roles this way).
                # The bot has those permissions at guild level, so its own
                # overwrite only needs the basics.
                log.warning(
                    "[%s] retrying ticket creation with a minimal bot overwrite (50013)",
                    guild.name,
                )
                overwrites[me] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                )
                try:
                    channel = await guild.create_text_channel(
                        name=name,
                        category=category,
                        overwrites=overwrites,
                        topic=topic,
                        reason=f"Ticket #{number:04d} opened by {author} ({author.id})",
                    )
                except discord.Forbidden as retry_exc:
                    log.warning(
                        "[%s] create_text_channel forbidden for %s: %s (code %s)",
                        guild.name,
                        author,
                        retry_exc.text,
                        retry_exc.code,
                    )
                    raise retry_exc from exc

            ticket = Ticket(
                channel_id=channel.id,
                guild_id=guild.id,
                number=number,
                type_key=ttype.key,
                owner_id=author.id,
                created_at=utcnow_iso(),
                reason=reason.strip()[:2000],
            )
            config.counter = number
            config.total_opened += 1
            self.store.add_ticket(ticket)
            await self.store.save()
            self.tickets_opened += 1

        log.info(
            "[%s] Ticket #%04d opened by %s (%s) - %s",
            guild.name,
            number,
            author,
            author.id,
            ttype.label,
        )

        mentions = [author]
        roles_to_ping = [
            role for role in support_roles if role is not None and role.id in ttype.ping_role_ids
        ]
        content_parts = [author.mention]
        if roles_to_ping:
            content_parts.append(" ".join(role.mention for role in roles_to_ping))

        try:
            await channel.send(
                content=" ".join(content_parts),
                embed=self.ticket_embed(ticket, config, guild),
                view=self.control_view,
                allowed_mentions=discord.AllowedMentions(users=mentions, roles=roles_to_ping),
            )
        except discord.HTTPException:
            log.warning("Ticket #%04d was created but the intro message failed.", number)
        return channel

    def _resolve_category(
        self,
        guild: discord.Guild,
        ttype: TicketType,
        config: GuildConfig,
    ) -> discord.CategoryChannel | None:
        category_id = ttype.category_id or config.category_id
        if category_id is None:
            raise TicketError(
                "No ticket category is configured yet. A moderator can run "
                "`/ticket setup` or `/ticket category` first."
            )
        category = guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            raise TicketError(
                "The configured ticket category no longer exists. "
                "A moderator can pick a new one with `/ticket category`."
            )

        me = guild.me
        if me is not None:
            missing = [
                name
                for name, allowed in (
                    ("Manage Channels", me.guild_permissions.manage_channels),
                    ("Manage Roles", me.guild_permissions.manage_roles),
                )
                if not allowed
            ]
            if missing:
                raise TicketError(
                    "I am missing the server permission(s): **"
                    + ", ".join(missing)
                    + "**.\nGo to **Server Settings → Roles → my role** and enable them."
                )

            perms = category.permissions_for(me)
            missing = [
                name
                for name, allowed in (
                    ("View Channel", perms.view_channel),
                    ("Send Messages", perms.send_messages),
                    ("Read Message History", perms.read_message_history),
                    ("Embed Links", perms.embed_links),
                    ("Attach Files", perms.attach_files),
                    ("Manage Messages", perms.manage_messages),
                    ("Manage Channels", perms.manage_channels),
                )
                if not allowed
            ]
            if missing:
                raise TicketError(
                    f"I cannot create channels in **{category.name}**: missing **"
                    + ", ".join(missing)
                    + "**.\nEdit that category → **Permissions** → add my role (or the bot "
                    "member) with **View Channel**, **Manage Channels** and **Manage Messages**."
                )
        return category

    def ticket_embed(
        self, ticket: Ticket, config: GuildConfig, guild: discord.Guild
    ) -> discord.Embed:
        ttype = config.find_type(ticket.type_key)
        type_label = ttype.label if ttype else ticket.type_key

        embed = discord.Embed(
            title=f"Ticket #{ticket.number:04d} — {type_label}",
            description=(
                "Thanks for the details. A staff member will reply soon — "
                "add anything else in this channel.\n\n"
                "Use the buttons below to claim, add a member, or close the ticket."
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Opened by", value=f"<@{ticket.owner_id}>", inline=True)
        embed.add_field(name="Opened", value=relative_time(ticket.created_at), inline=True)
        if ticket.claimed_by:
            embed.add_field(name="Claimed by", value=f"<@{ticket.claimed_by}>", inline=True)
        else:
            embed.add_field(name="Claimed by", value="Nobody yet", inline=True)
        if ticket.reason:
            embed.add_field(
                name="Issue",
                value=truncate(ticket.reason, 1024),
                inline=False,
            )
        embed.set_footer(text=f"{guild.name} • ticket #{ticket.number:04d}")
        return embed

    # -- ticket actions ----------------------------------------------------- #

    async def claim_from_button(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self.reply_error(interaction, "Tickets only work inside a server.")
            return
        log.info(
            "Claim attempt: guild=%s channel=%s user=%s (%s) message=%s",
            guild.id,
            interaction.channel_id,
            interaction.user,
            interaction.user.id,
            interaction.message.id if interaction.message else None,
        )
        config = self.store.config(guild.id)
        if not self.is_staff(interaction.user, config):
            await self.reply_error(interaction, "Only support staff can claim tickets.")
            return

        ticket = self.store.ticket(interaction.channel_id) if interaction.channel_id else None
        if ticket is None:
            await self.reply_error(interaction, "This channel is not a tracked ticket.")
            return
        if ticket.claimed_by == interaction.user.id:
            await self.reply_error(interaction, "You already claimed this ticket.", title="Already claimed")
            return

        # Acknowledge within Discord's 3 second deadline *before* any disk I/O,
        # otherwise the click fails with "This interaction failed" and the bot
        # looks like it ignored the button.
        from_button = interaction.message is not None
        await interaction.response.defer(ephemeral=not from_button)

        ticket.claimed_by = interaction.user.id
        await self.store.save()

        embed = self.ticket_embed(ticket, config, guild)
        announcement = f"{interaction.user.mention} is handling this ticket."
        if from_button:
            try:
                await interaction.edit_original_response(embed=embed, view=self.control_view)
            except discord.HTTPException:
                log.warning("Could not update the ticket embed", exc_info=True)
        else:
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.HTTPException:
                log.warning("Could not send the claim confirmation", exc_info=True)

        if isinstance(interaction.channel, discord.TextChannel):
            try:
                await interaction.channel.send(
                    announcement,
                    allowed_mentions=discord.AllowedMentions(users=[interaction.user]),
                )
            except discord.HTTPException:
                log.warning("Could not announce the claim", exc_info=True)

    async def add_member_to_ticket(self, interaction: discord.Interaction, raw: str) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.channel, discord.TextChannel):
            return
        config = self.store.config(guild.id)
        if not self.is_staff(interaction.user, config):
            await self.reply_error(interaction, "Only support staff can add members to a ticket.")
            return

        match = re.search(r"\d{15,25}", raw)
        if match is None:
            await self.reply_error(
                interaction,
                "Give me a user ID or an @mention (e.g. `123456789012345678`).",
                title="Invalid user",
            )
            return
        user_id = int(match.group())

        await interaction.response.defer(ephemeral=True, thinking=True)

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.HTTPException):
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="Unknown member",
                        description="I could not find that member in this server.",
                        color=0xED4245,
                    ),
                    ephemeral=True,
                )
                return

        try:
            await interaction.channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                reason=f"Added to ticket by {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Missing permissions",
                    description="I need **Manage Roles** and **Manage Channels** to add members.",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=discord.Embed(
                title="Member added",
                description=f"{member.mention} can now see this ticket.",
                color=0x57F287,
            ),
            ephemeral=True,
        )
        await interaction.channel.send(
            f"Added {member.mention} to the ticket.",
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )

    async def close_from_interaction(
        self, interaction: discord.Interaction, reason: str
    ) -> None:
        guild = interaction.guild
        if guild is None or interaction.channel_id is None:
            return
        ticket = self.store.ticket(interaction.channel_id)
        if ticket is None:
            await self.reply_error(interaction, "This channel is not a tracked ticket.")
            return

        config = self.store.config(guild.id)
        if not self.can_close(interaction.user, ticket, config):
            await self.reply_error(
                interaction,
                "Only the ticket owner or support staff can close this ticket.",
            )
            return

        if ticket.channel_id in self._closing:
            await self.reply_error(interaction, "This ticket is already closing.", title="Already closing")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            transcript = await self.close_ticket(guild, ticket, interaction.user, reason, config)
        except TicketError as exc:
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="Could not close the ticket", description=str(exc), color=0xED4245
                    ),
                    ephemeral=True,
                )
            return
        except discord.HTTPException as exc:
            log.exception("Could not close ticket #%04d", ticket.number)
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="Could not close the ticket",
                        description=f"Discord rejected an action: {exc}",
                        color=0xED4245,
                    ),
                    ephemeral=True,
                )
            return

        if transcript == "log":
            description = "The transcript was saved to the log channel and this channel will be removed."
        elif transcript == "dm":
            description = (
                "The log channel is unavailable, so I DMed the transcript to the ticket owner. "
                "This channel will be removed."
            )
        else:
            description = (
                "This channel will be removed, but I could not save the transcript. "
                "Ask an admin to give me **View Channel**, **Send Messages** and "
                "**Attach Files** in the log channel."
            )
        color = 0x57F287 if transcript else 0xFEE75C
        try:
            await interaction.followup.send(
                embed=discord.Embed(title="Ticket closed", description=description, color=color),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            # The channel may have been deleted by someone else in the meantime.
            log.warning("Could not send the close confirmation: %s", exc)

    async def close_ticket(
        self,
        guild: discord.Guild,
        ticket: Ticket,
        closed_by: discord.abc.User,
        reason: str,
        config: GuildConfig,
    ) -> str | None:
        """Close a ticket and schedule the channel deletion.

        Returns where the transcript went: ``"log"``, ``"dm"`` or ``None`` when
        it could not be delivered at all. The channel is deleted in a background
        task so the caller can answer the interaction *before* it disappears.
        """
        channel = guild.get_channel(ticket.channel_id)
        if not isinstance(channel, discord.TextChannel):
            self.store.remove_ticket(ticket.channel_id)
            await self.store.save()
            raise TicketError("The ticket channel no longer exists.")

        if ticket.channel_id in self._closing:
            raise TicketError("This ticket is already closing.")
        self._closing.add(ticket.channel_id)
        scheduled = False
        try:
            # Remove it first so a double click cannot close twice.
            self.store.remove_ticket(ticket.channel_id)
            await self.store.save()
            self.tickets_closed += 1

            ttype = config.find_type(ticket.type_key)
            type_label = ttype.label if ttype else ticket.type_key
            messages = await self._collect_messages(channel)
            transcript = render_transcript(
                guild_name=guild.name,
                channel_name=getattr(channel, "name", str(channel.id)),
                ticket=ticket,
                type_label=type_label,
                messages=messages,
            )

            log_embed = discord.Embed(
                title=f"Ticket #{ticket.number:04d} closed",
                color=0xED4245,
                timestamp=discord.utils.utcnow(),
            )
            log_embed.add_field(name="Category", value=type_label, inline=True)
            log_embed.add_field(name="Opened by", value=f"<@{ticket.owner_id}>", inline=True)
            log_embed.add_field(name="Closed by", value=f"{closed_by.mention}", inline=True)
            log_embed.add_field(name="Open for", value=format_duration(ticket.created_at), inline=True)
            log_embed.add_field(name="Messages", value=str(len(messages)), inline=True)
            if ticket.reason:
                log_embed.add_field(
                    name="Reported issue", value=truncate(ticket.reason, 1024), inline=False
                )
            log_embed.add_field(name="Reason", value=reason or "No reason given", inline=False)
            if ticket.claimed_by:
                log_embed.add_field(name="Claimed by", value=f"<@{ticket.claimed_by}>", inline=True)

            filename = f"ticket-{ticket.number:04d}.html"
            transcript_target: str | None = None

            log_channel = guild.get_channel(config.log_channel_id) if config.log_channel_id else None
            if isinstance(log_channel, discord.TextChannel):
                try:
                    await log_channel.send(
                        embed=log_embed,
                        file=discord.File(io.BytesIO(transcript), filename=filename),
                    )
                    transcript_target = "log"
                except discord.HTTPException as exc:
                    log.warning("Could not deliver the transcript to the log channel: %s", exc)

            if transcript_target is None:
                # Fall back to DMing the ticket owner so the record is not lost.
                try:
                    owner = guild.get_member(ticket.owner_id) or await self.fetch_user(ticket.owner_id)
                    await owner.send(
                        embed=log_embed,
                        file=discord.File(io.BytesIO(transcript), filename=filename),
                    )
                    transcript_target = "dm"
                except (discord.HTTPException, discord.NotFound):
                    log.warning("Could not deliver ticket #%04d transcript anywhere.", ticket.number)

            closing_embed = discord.Embed(
                title="Ticket closed",
                description=(
                    f"Closed by {closed_by.mention}.\n"
                    f"**Reason:** {reason or 'No reason given'}\n\n"
                    f"This channel disappears in {CLOSE_DELAY_SECONDS} seconds."
                ),
                color=0xED4245,
            )
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=closing_embed)

            # Delete in the background: the interaction that asked for the close
            # must be answered while its channel still exists.
            asyncio.create_task(
                self._delete_channel_later(channel, closed_by, ticket),
                name=f"ticket-delete-{ticket.channel_id}",
            )
            scheduled = True

            log.info(
                "[%s] Ticket #%04d closed by %s (%s): %s",
                guild.name,
                ticket.number,
                closed_by,
                closed_by.id,
                reason or "no reason",
            )
            return transcript_target
        finally:
            if not scheduled:
                self._closing.discard(ticket.channel_id)

    async def _delete_channel_later(
        self,
        channel: discord.TextChannel,
        closed_by: discord.abc.User,
        ticket: Ticket,
    ) -> None:
        try:
            await asyncio.sleep(CLOSE_DELAY_SECONDS)
            try:
                await channel.delete(reason=f"Ticket closed by {closed_by} ({closed_by.id})")
            except discord.NotFound:
                pass
            except discord.Forbidden as exc:
                log.warning(
                    "Could not delete the channel of ticket #%04d: %s. "
                    "The bot needs Manage Channels.",
                    ticket.number,
                    exc,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected error while deleting the channel of ticket #%04d", ticket.number)
        finally:
            self._closing.discard(ticket.channel_id)

    async def _collect_messages(self, channel: discord.TextChannel) -> list[discord.Message]:
        messages: list[discord.Message] = []
        try:
            async for message in channel.history(limit=MAX_TRANSCRIPT_MESSAGES, oldest_first=True):
                messages.append(message)
        except discord.HTTPException as exc:
            log.warning("Could not read the ticket history: %s", exc)
        return messages


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #


class TicketCog(commands.Cog, name="Tickets"):
    def __init__(self, bot: TicketBot) -> None:
        self.bot = bot

    ticket = app_commands.Group(
        name="ticket",
        description="Create and manage support tickets.",
        guild_only=True,
    )
    support = app_commands.Group(
        name="support",
        description="Manage the roles that can answer tickets.",
        parent=ticket,
    )
    type_group = app_commands.Group(
        name="type",
        description="Manage the categories shown on the ticket panel.",
        parent=ticket,
    )

    # -- helpers ------------------------------------------------------------ #

    def _config(self, interaction: discord.Interaction) -> GuildConfig:
        assert interaction.guild_id is not None
        return self.bot.store.config(interaction.guild_id)

    async def _error(
        self, interaction: discord.Interaction, title: str, message: str
    ) -> None:
        embed = discord.Embed(title=title, description=message, color=0xED4245)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _ok(
        self, interaction: discord.Interaction, title: str, message: str
    ) -> None:
        embed = discord.Embed(title=title, description=message, color=0x57F287)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _require_manager(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await self._error(interaction, "Server only", "This command only works inside a server.")
            return False
        if not isinstance(interaction.user, discord.Member) or not self.bot.is_manager(interaction.user):
            await self._error(
                interaction,
                "Missing permission",
                "You need the **Manage Server** permission to change ticket settings.",
            )
            return False
        return True

    # -- setup -------------------------------------------------------------- #

    @ticket.command(name="setup", description="Set up the ticket system in one step.")
    @app_commands.describe(
        category="Category where ticket channels will be created.",
        panel="Channel where the ticket panel will be posted.",
        log_channel="Channel that receives closed-ticket transcripts.",
        support="Role that can see and answer tickets.",
    )
    @app_commands.rename(log_channel="log")
    @app_commands.default_permissions(manage_guild=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        panel: discord.TextChannel,
        log_channel: discord.TextChannel | None = None,
        support: discord.Role | None = None,
    ) -> None:
        if not await self._require_manager(interaction):
            return

        config = self._config(interaction)
        config.category_id = category.id
        config.log_channel_id = log_channel.id if log_channel else config.log_channel_id
        if support is not None and support.id not in config.support_role_ids:
            config.support_role_ids.append(support.id)
        await self.bot.store.save()

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.bot.post_panel(panel, config)
        except TicketError as exc:
            await self._error(interaction, "Panel not posted", str(exc))
            return
        except discord.Forbidden:
            await self._error(
                interaction,
                "Panel not posted",
                f"I need **View Channel**, **Send Messages**, **Embed Links** and "
                f"**Read Message History** in {panel.mention}.",
            )
            return
        except discord.HTTPException as exc:
            await self._error(
                interaction,
                "Panel not posted",
                f"Discord returned an error: {exc.text or exc}",
            )
            return

        support_text = " ".join(f"<@&{rid}>" for rid in config.support_role_ids) or "not set"
        embed = discord.Embed(
            title="Ticket system ready",
            description=f"The panel is live in {message.channel.mention}.",
            color=0x57F287,
        )
        embed.add_field(name="Ticket category", value=category.mention, inline=True)
        embed.add_field(name="Transcript log", value=log_channel.mention if log_channel else "not set", inline=True)
        embed.add_field(name="Support roles", value=support_text, inline=True)
        embed.add_field(
            name="Next steps",
            value=(
                "• Move my role above the support roles.\n"
                "• Members pick a category on the panel to open a ticket.\n"
                "• Run `/ticket type list` to see the categories."
            ),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ticket.command(name="panel", description="Post (or refresh) the ticket panel.")
    @app_commands.describe(channel="Channel to post the panel in (defaults to here).")
    @app_commands.default_permissions(manage_guild=True)
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        if not await self._require_manager(interaction):
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await self._error(interaction, "Wrong channel", "Pick a normal text channel.")
            return

        config = self._config(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.bot.post_panel(target, config)
        except TicketError as exc:
            await self._error(interaction, "Panel not posted", str(exc))
            return
        except discord.Forbidden:
            await self._error(
                interaction,
                "Panel not posted",
                f"I cannot post in {target.mention}. Check my channel permissions.",
            )
            return
        except discord.HTTPException as exc:
            await self._error(
                interaction,
                "Panel not posted",
                f"Discord returned an error: {exc.text or exc}",
            )
            return
        await self._ok(
            interaction,
            "Panel posted",
            f"The ticket panel is now in {message.channel.mention}.\n"
            f"**Categories:** {len(config.types)}",
        )

    @ticket.command(name="log", description="Set the channel that receives transcripts.")
    @app_commands.describe(channel="Transcript log channel. Leave empty to disable logging.")
    @app_commands.default_permissions(manage_guild=True)
    async def log_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        if not await self._require_manager(interaction):
            return
        config = self._config(interaction)
        config.log_channel_id = channel.id if channel else None
        await self.bot.store.save()
        await self._ok(
            interaction,
            "Transcript log updated",
            f"Closed tickets will be logged in {channel.mention}." if channel else "Transcript logging disabled.",
        )

    @ticket.command(name="category", description="Set the category where ticket channels are created.")
    @app_commands.describe(category="Category for new ticket channels.")
    @app_commands.default_permissions(manage_guild=True)
    async def category(self, interaction: discord.Interaction, category: discord.CategoryChannel) -> None:
        if not await self._require_manager(interaction):
            return
        config = self._config(interaction)
        config.category_id = category.id
        await self.bot.store.save()
        await self._ok(
            interaction,
            "Ticket category updated",
            f"New tickets will be created under **{category.name}**.",
        )

    @ticket.command(name="toggle", description="Enable or disable the ticket system.")
    @app_commands.describe(enabled="false stops new tickets from being opened.")
    @app_commands.default_permissions(manage_guild=True)
    async def toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not await self._require_manager(interaction):
            return
        config = self._config(interaction)
        config.enabled = enabled
        await self.bot.store.save()
        detail = (
            "Members can open tickets again."
            if enabled
            else "The panel stays posted, but new tickets are refused."
        )
        await self._ok(
            interaction,
            "Ticket system updated",
            f"{yes_no(enabled)}\n{detail}",
        )

    @ticket.command(name="max", description="Set how many tickets one member may have open.")
    @app_commands.describe(amount="Maximum open tickets per member (1-25).")
    @app_commands.default_permissions(manage_guild=True)
    async def max_tickets(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 25]
    ) -> None:
        if not await self._require_manager(interaction):
            return
        config = self._config(interaction)
        config.max_open_per_user = int(amount)
        await self.bot.store.save()
        await self._ok(
            interaction,
            "Limit updated",
            f"Each member may have at most **{config.max_open_per_user}** open ticket(s).",
        )

    # -- support roles ------------------------------------------------------ #

    @support.command(name="add", description="Allow a role to see and answer tickets.")
    @app_commands.describe(role="Role to add to the support team.")
    @app_commands.default_permissions(manage_guild=True)
    async def support_add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not await self._require_manager(interaction):
            return
        if role.is_default():
            await self._error(
                interaction,
                "Not allowed",
                "The **@everyone** role cannot be the support team — that would let everyone see every ticket.",
            )
            return
        config = self._config(interaction)
        if role.id in config.support_role_ids:
            await self._error(interaction, "Already added", f"{role.mention} is already a support role.")
            return
        config.support_role_ids.append(role.id)
        await self.bot.store.save()
        await self._ok(
            interaction,
            "Support role added",
            f"{role.mention} can now see and answer tickets.\n"
            "Refresh the panel with `/ticket panel` to update its footer.",
        )

    @support.command(name="remove", description="Remove a role from the support team.")
    @app_commands.describe(role="Role to remove.")
    @app_commands.default_permissions(manage_guild=True)
    async def support_remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not await self._require_manager(interaction):
            return
        config = self._config(interaction)
        if role.id not in config.support_role_ids:
            await self._error(interaction, "Not a support role", f"{role.mention} is not on the support team.")
            return
        config.support_role_ids.remove(role.id)
        await self.bot.store.save()
        await self._ok(interaction, "Support role removed", f"{role.mention} can no longer see new tickets.")

    @support.command(name="list", description="Show the support roles.")
    async def support_list(self, interaction: discord.Interaction) -> None:
        config = self._config(interaction)
        if config.support_role_ids:
            roles = "\n".join(f"• <@&{rid}>" for rid in config.support_role_ids)
        else:
            roles = "No support roles configured. Anyone with **Manage Channels** can answer tickets."
        await self._ok(interaction, "Support roles", roles)

    # -- ticket types ------------------------------------------------------- #

    async def _type_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        config = self.bot.store.config(interaction.guild_id or 0)
        needle = current.lower().strip()
        choices = [
            app_commands.Choice(name=f"{ttype.label} ({ttype.key})", value=ttype.key)
            for ttype in config.types
            if not needle or needle in ttype.key.lower() or needle in ttype.label.lower()
        ]
        return choices[:25]

    @type_group.command(name="add", description="Add a category to the ticket panel.")
    @app_commands.describe(
        label="Name shown on the panel, e.g. 'Bug Report'.",
        description="Short explanation shown under the name.",
        emoji="Optional emoji shown next to the name.",
        category="Optional category just for this type.",
        ping_role="Optional role pinged when this ticket type is opened.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def type_add(
        self,
        interaction: discord.Interaction,
        label: str,
        description: str | None = None,
        emoji: str | None = None,
        category: discord.CategoryChannel | None = None,
        ping_role: discord.Role | None = None,
    ) -> None:
        if not await self._require_manager(interaction):
            return
        if emoji:
            try:
                discord.SelectOption(label="check", value="check", emoji=emoji)
            except (TypeError, ValueError):
                await self._error(
                    interaction,
                    "Invalid emoji",
                    "That is not a valid emoji. Use a normal emoji like 🐞 or a custom one like `<:name:123456789012345678>`.",
                )
                return
        config = self._config(interaction)
        if len(config.types) >= MAX_TYPES:
            await self._error(
                interaction,
                "Too many categories",
                f"The panel supports at most {MAX_TYPES} categories.",
            )
            return

        key = slugify(label, "ticket")
        base = key
        suffix = 2
        while config.find_type(key) is not None:
            key = f"{base}-{suffix}"
            suffix += 1

        config.types.append(
            TicketType(
                key=key,
                label=label,
                description=description or "",
                emoji=emoji,
                category_id=category.id if category else None,
                ping_role_ids=[ping_role.id] if ping_role else [],
            )
        )
        await self.bot.store.save()
        await self._ok(
            interaction,
            "Category added",
            f"**{label}** is now on the panel (key: `{key}`).\n"
            "Run `/ticket panel` to refresh the panel message.",
        )

    @type_group.command(name="remove", description="Remove a category from the panel.")
    @app_commands.describe(key="Category key (use /ticket type list to see them).")
    @app_commands.autocomplete(key=_type_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def type_remove(self, interaction: discord.Interaction, key: str) -> None:
        if not await self._require_manager(interaction):
            return
        config = self._config(interaction)
        ttype = config.find_type(key)
        if ttype is None:
            await self._error(interaction, "Unknown category", f"No category has the key `{key}`.")
            return
        if len(config.types) <= 1:
            await self._error(
                interaction,
                "Cannot remove",
                "At least one ticket category must remain on the panel.",
            )
            return
        config.types.remove(ttype)
        await self.bot.store.save()
        await self._ok(
            interaction,
            "Category removed",
            f"**{ttype.label}** was removed.\nRun `/ticket panel` to refresh the panel.",
        )

    @type_group.command(name="list", description="Show the ticket categories.")
    async def type_list(self, interaction: discord.Interaction) -> None:
        config = self._config(interaction)
        lines = []
        for ttype in config.types:
            emoji = f"{ttype.emoji} " if ttype.emoji else ""
            extra = []
            if ttype.category_id:
                extra.append(f"category <#{ttype.category_id}>")
            if ttype.ping_role_ids:
                extra.append("ping " + " ".join(f"<@&{rid}>" for rid in ttype.ping_role_ids))
            suffix = f" — {', '.join(extra)}" if extra else ""
            lines.append(f"**{ttype.label}** (`{ttype.key}`) {emoji}{suffix}")
        embed = discord.Embed(
            title="Ticket categories",
            description="\n".join(lines) or "No categories configured.",
            color=0x5865F2,
        )
        embed.set_footer(text=f"{len(config.types)} of {MAX_TYPES} used")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- config / stats ----------------------------------------------------- #

    @ticket.command(name="config", description="Show the current ticket configuration.")
    async def config(self, interaction: discord.Interaction) -> None:
        config = self._config(interaction)
        category = f"<#{config.category_id}>" if config.category_id else "not set"
        log_channel = f"<#{config.log_channel_id}>" if config.log_channel_id else "not set"
        panel = f"<#{config.panel_channel_id}>" if config.panel_channel_id else "not posted"
        support = " ".join(f"<@&{rid}>" for rid in config.support_role_ids) or "not set"
        open_count = len(self.bot.store.tickets_for_guild(interaction.guild_id or 0))

        embed = discord.Embed(
            title="Ticket configuration",
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Status", value=yes_no(config.enabled), inline=True)
        embed.add_field(name="Open tickets", value=str(open_count), inline=True)
        embed.add_field(name="Total opened", value=str(config.total_opened), inline=True)
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Panel", value=panel, inline=True)
        embed.add_field(name="Transcript log", value=log_channel, inline=True)
        embed.add_field(name="Support roles", value=support, inline=False)
        embed.add_field(
            name="Categories",
            value=", ".join(f"`{ttype.key}`" for ttype in config.types),
            inline=False,
        )
        embed.add_field(name="Max open per member", value=str(config.max_open_per_user), inline=True)
        embed.set_footer(text=f"{len(config.types)} ticket categories")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ticket.command(name="diagnose", description="Check permissions and setup for problems.")
    @app_commands.default_permissions(manage_guild=True)
    async def diagnose(self, interaction: discord.Interaction) -> None:
        if not await self._require_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        config = self._config(interaction)
        me = guild.me
        if me is None:
            await self._error(
                interaction,
                "Diagnostics failed",
                "I could not resolve my own member object. Re-invite the bot.",
            )
            return

        problems: list[str] = []
        embed = discord.Embed(
            title="Ticket system diagnostics",
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )

        # 1. Server-level permissions.
        perms = me.guild_permissions
        required = [
            ("View Channels", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Read Message History", perms.read_message_history),
            ("Embed Links", perms.embed_links),
            ("Attach Files", perms.attach_files),
            ("Manage Messages", perms.manage_messages),
            ("Manage Channels", perms.manage_channels),
            ("Manage Roles", perms.manage_roles),
        ]
        lines = []
        for name, allowed in required:
            lines.append(f"{'✅' if allowed else '❌'} {name}")
            if not allowed:
                problems.append(f"Give my role **{name}** in Server Settings → Roles")
        embed.add_field(name="Server permissions", value="\n".join(lines), inline=True)
        embed.add_field(
            name="My highest role",
            value=f"{me.top_role.mention}\nposition **{me.top_role.position}**",
            inline=True,
        )

        # 2. Ticket category.
        category = guild.get_channel(config.category_id) if config.category_id else None
        if config.category_id is None:
            category_text = "❌ No category configured.\nRun `/ticket category category:#...`."
            problems.append("Set a ticket category with `/ticket category`")
        elif not isinstance(category, discord.CategoryChannel):
            category_text = "❌ The configured category no longer exists.\nRun `/ticket category`."
            problems.append("Choose an existing ticket category")
        else:
            cperms = category.permissions_for(me)
            cat_checks = [
                ("View Channel", cperms.view_channel),
                ("Send Messages", cperms.send_messages),
                ("Read Message History", cperms.read_message_history),
                ("Embed Links", cperms.embed_links),
                ("Attach Files", cperms.attach_files),
                ("Manage Messages", cperms.manage_messages),
                ("Manage Channels", cperms.manage_channels),
            ]
            cat_lines = [f"{'✅' if ok else '❌'} {name}" for name, ok in cat_checks]
            missing = [name for name, ok in cat_checks if not ok]
            if missing:
                problems.append(
                    "Give my role "
                    + ", ".join(f"**{name}**" for name in missing)
                    + f" in {category.mention}"
                )
            category_text = f"{category.mention}\n" + "\n".join(cat_lines)
        embed.add_field(name="Ticket category", value=category_text, inline=False)

        # 3. Support roles (and whether staff can actually be assigned them).
        role_ids = list(
            dict.fromkeys(
                config.support_role_ids
                + [rid for ttype in config.types for rid in ttype.ping_role_ids]
            )
        )
        role_lines = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None:
                role_lines.append(f"❌ <@&{role_id}> no longer exists")
            elif role.is_default():
                role_lines.append(f"❌ <@&{role_id}> is @everyone — remove it")
                problems.append("Remove **@everyone** from the support roles")
            elif role.managed and role.tags is not None and role.tags.bot_id == me.id:
                role_lines.append(f"⚠️ {role.mention} is my own bot role")
                problems.append(
                    "Add a separate human **Support** role with `/ticket support add` "
                    "(Discord does not let staff have a bot's own role)"
                )
            elif role.position > me.top_role.position:
                role_lines.append(f"ℹ️ {role.mention} is above my role (fine for tickets)")
            else:
                role_lines.append(f"✅ {role.mention}")
        embed.add_field(
            name="Support roles",
            value="\n".join(role_lines) or "No support roles configured.",
            inline=False,
        )

        # 4. Panel and log channels.
        channel_lines = []
        for label, channel_id, needed in (
            (
                "Panel",
                config.panel_channel_id,
                [("Send Messages", "send_messages"), ("Embed Links", "embed_links")],
            ),
            (
                "Transcript log",
                config.log_channel_id,
                [
                    ("Send Messages", "send_messages"),
                    ("Embed Links", "embed_links"),
                    ("Attach Files", "attach_files"),
                ],
            ),
        ):
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is None:
                channel_lines.append(f"❌ {label}: not configured")
                continue
            channel_perms = channel.permissions_for(me)
            missing = [name for name, attr in needed if not getattr(channel_perms, attr)]
            if missing:
                channel_lines.append(f"❌ {label}: {channel.mention} missing {', '.join(missing)}")
                problems.append(f"Give my role {', '.join(missing)} in {channel.mention}")
            else:
                channel_lines.append(f"✅ {label}: {channel.mention}")
        embed.add_field(name="Channels", value="\n".join(channel_lines), inline=False)

        if problems:
            embed.color = 0xED4245
            shown = problems[:6]
            extra = f"\n…and {len(problems) - len(shown)} more" if len(problems) > 6 else ""
            embed.add_field(
                name=f"Verdict — {len(problems)} problem(s) found",
                value="\n".join(f"• {problem}" for problem in shown) + extra,
                inline=False,
            )
        else:
            embed.add_field(
                name="Verdict",
                value=(
                    "✅ Everything looks correct. Try opening a ticket.\n"
                    "If it still fails, a channel overwrite on the category is blocking me."
                ),
                inline=False,
            )
        embed.set_footer(text="Run this again after changing permissions.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ticket.command(name="stats", description="Show ticket statistics.")
    async def stats(self, interaction: discord.Interaction) -> None:
        config = self._config(interaction)
        tickets = self.bot.store.tickets_for_guild(interaction.guild_id or 0)
        by_type: dict[str, int] = {}
        claimed = 0
        for ticket in tickets:
            by_type[ticket.type_key] = by_type.get(ticket.type_key, 0) + 1
            if ticket.claimed_by:
                claimed += 1

        type_lines = []
        for ttype in config.types:
            type_lines.append(f"• **{ttype.label}**: {by_type.get(ttype.key, 0)} open")
        if not type_lines:
            type_lines = ["No categories configured."]

        embed = discord.Embed(
            title="Ticket statistics",
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Currently open", value=str(len(tickets)), inline=True)
        embed.add_field(name="Claimed", value=str(claimed), inline=True)
        embed.add_field(name="Total opened", value=str(config.total_opened), inline=True)
        embed.add_field(name="Open by category", value="\n".join(type_lines), inline=False)
        embed.set_footer(text=f"This session: {self.bot.tickets_opened} opened, {self.bot.tickets_closed} closed")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ticket.command(name="reset", description="Reset all ticket settings for this server.")
    @app_commands.describe(confirm="Set to true to actually reset.")
    @app_commands.default_permissions(manage_guild=True)
    async def reset(self, interaction: discord.Interaction, confirm: bool = False) -> None:
        if not await self._require_manager(interaction):
            return
        if not confirm:
            await self._error(
                interaction,
                "Confirmation required",
                "This deletes the configuration and forgets all open tickets "
                "(channels are not deleted).\nRun `/ticket reset confirm:true` to continue.",
            )
            return
        assert interaction.guild_id is not None
        self.bot.store.reset(interaction.guild_id)
        await self.bot.store.save()
        await self._ok(
            interaction,
            "Configuration reset",
            "Ticket settings were reset to defaults. Existing ticket channels were left alone.\n"
            "Run `/ticket setup` again to reconfigure.",
        )

    # -- per-ticket commands ------------------------------------------------ #

    @ticket.command(name="close", description="Close the ticket you are in.")
    @app_commands.describe(reason="Reason for closing (stored in the transcript).")
    async def close(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        await self.bot.close_from_interaction(interaction, (reason or "").strip())

    @ticket.command(name="claim", description="Claim the ticket you are in.")
    async def claim(self, interaction: discord.Interaction) -> None:
        await self.bot.claim_from_button(interaction)

    @ticket.command(name="add", description="Add a member to the ticket you are in.")
    @app_commands.describe(user="Member to add to this ticket.")
    async def add(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None:
            return
        config = self._config(interaction)
        if not self.bot.is_staff(interaction.user, config):
            await self._error(
                interaction,
                "Not allowed",
                "Only support staff can add members to a ticket.",
            )
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await self._error(interaction, "Wrong channel", "Use this inside a ticket channel.")
            return
        if self.bot.store.ticket(interaction.channel_id) is None:
            await self._error(interaction, "Not a ticket", "This channel is not a tracked ticket.")
            return
        try:
            await interaction.channel.set_permissions(
                user,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                reason=f"Added to ticket by {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            await self._error(
                interaction,
                "Missing permissions",
                "I need **Manage Roles** and **Manage Channels** to add members.",
            )
            return
        await self._ok(interaction, "Member added", f"{user.mention} can now see this ticket.")

    @ticket.command(name="remove", description="Remove a member from the ticket you are in.")
    @app_commands.describe(user="Member to remove from this ticket.")
    async def remove(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None:
            return
        config = self._config(interaction)
        if not self.bot.is_staff(interaction.user, config):
            await self._error(interaction, "Not allowed", "Only support staff can remove members.")
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await self._error(interaction, "Wrong channel", "Use this inside a ticket channel.")
            return
        ticket = self.bot.store.ticket(interaction.channel_id)
        if ticket is None:
            await self._error(interaction, "Not a ticket", "This channel is not a tracked ticket.")
            return
        if user.id == ticket.owner_id:
            await self._error(
                interaction,
                "Not allowed",
                "The ticket owner cannot be removed. Close the ticket instead.",
            )
            return
        try:
            await interaction.channel.set_permissions(user, overwrite=None)
        except discord.Forbidden:
            await self._error(
                interaction,
                "Missing permissions",
                "I need **Manage Roles** and **Manage Channels** to remove members.",
            )
            return
        await self._ok(interaction, "Member removed", f"{user.mention} can no longer see this ticket.")

    @ticket.command(name="rename", description="Rename the ticket channel.")
    @app_commands.describe(name="New channel name.")
    async def rename(self, interaction: discord.Interaction, name: str) -> None:
        if interaction.guild is None:
            return
        config = self._config(interaction)
        if not self.bot.is_staff(interaction.user, config):
            await self._error(interaction, "Not allowed", "Only support staff can rename tickets.")
            return
        ticket = self.bot.store.ticket(interaction.channel_id or 0)
        if ticket is None or not isinstance(interaction.channel, discord.TextChannel):
            await self._error(interaction, "Not a ticket", "Use this inside a ticket channel.")
            return
        new_name = channel_name(name, f"{ticket.type_key}-{ticket.number:04d}")
        try:
            await interaction.channel.edit(name=new_name, reason=f"Renamed by {interaction.user}")
        except discord.Forbidden:
            await self._error(
                interaction,
                "Missing permissions",
                "I need **Manage Channels** to rename the ticket.",
            )
            return
        await self._ok(interaction, "Channel renamed", f"The ticket is now {interaction.channel.mention}.")

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need the **Manage Server** permission to use this."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "This command only works inside a server."
        else:
            log.exception("Slash command failed", exc_info=error)
            message = "Something went wrong while running that command. Check the console log."
        await self.bot.reply_error(interaction, message, title="Command failed")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    load_env_file(ENV_FILE)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / "ticket-bot.log",
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        )
    except OSError:
        log.warning("Could not create logs/ticket-bot.log; logging to the console only.")
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    token = (os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or "").strip()
    if not token:
        log.error("No bot token found. Copy .env.example to .env and set DISCORD_TOKEN.")
        sys.exit(1)
    if token in {"your-token-here", "PASTE_YOUR_TOKEN_HERE"}:
        log.error(
            ".env still contains the placeholder token. Open .env and replace "
            "DISCORD_TOKEN with the real token from the Developer Portal."
        )
        sys.exit(1)

    store = TicketStore(DATA_FILE)
    store.load()
    bot = TicketBot(store)

    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the token. Double-check DISCORD_TOKEN in .env.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord refused the login because a privileged intent is disabled. "
            "Enable it in the Developer Portal or remove it from the bot."
        )
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
