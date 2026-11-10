# Discord Ticket Bot

A self-hosted support ticket system for Discord. Members pick a category on a
panel and get a **private channel** where only they and your support team can
talk. Staff can claim tickets, add other members, and close them with a full
HTML transcript.

No database, no web server, no third-party services — just Python and
`discord.py`.

## Features

- **Ticket panel** with a dropdown of configurable categories (Support,
  Report a Member, Bug Report, …).
- **Issue popup**: picking a category opens a modal where the member describes
  their problem, and that text is saved on the ticket and shown in its embed.
- **Private ticket channels** created under a category; `@everyone` cannot see
  them.
- **Support roles** that can see and answer every ticket.
- **Claim** tickets so staff know who is handling them.
- **Add / remove members** from a ticket, rename the channel.
- **Close button** with an optional reason, plus `/ticket close`.
- **HTML transcripts** posted to a log channel, with a DM fallback if no log
  channel is configured.
- **Limits**: one open ticket per member by default (configurable).
- **Per-category routing**: a category can have its own channel category and a
  role pinged when a ticket of that type is opened.
- Everything is configured through `/ticket` slash commands.
- Settings survive restarts in `data/tickets.json`.

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | The whole bot. |
| `.env` | Your token and optional defaults. **Never share this file.** |
| `.env.example` | Template for `.env`. |
| `requirements.txt` | `discord.py`. |
| `run.bat` | Windows launcher (creates `.venv`, installs deps, starts the bot). |
| `data/tickets.json` | Created automatically; stores config and open tickets. |

---

## Setup

### 1. Create a new application

This must be a **new** bot application — do not reuse the honeypot bot's token.

1. Open <https://discord.com/developers/applications>
2. **New Application** → name it (e.g. `Ticket Bot`) → **Create**.
3. Left sidebar → **Bot**.

No privileged intents are required. Leave **Message Content Intent**,
**Server Members Intent** and **Presence Intent** off.

### 2. Installation settings

Left sidebar → **Installation**:

- **Installation Contexts**: enable **Guild Install** only.
- **Install Link**: **Discord Provided Link**.
- **Default Install Settings → Guild Install → Scopes**:
  - `bot`
  - `applications.commands`
- **Permissions**: `268561424`

That permission number is:

> View Channels, Send Messages, Manage Messages, Embed Links, Attach Files,
> Read Message History, Manage Channels, Manage Roles

**Save Changes.**

### 3. Invite the bot

Open the Install Link from the same page and pick your server. Or build the URL
yourself — replace `YOUR_CLIENT_ID` with the **Application ID** from the
**General Information** page:

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=268561424
```

### 4. Add your token

Copy the token from **Bot → Reset Token → Copy**, then open `.env`:

```
notepad "C:\Users\Lenovo\Documents\ticket-bot\.env"
```

Replace:

```dotenv
DISCORD_TOKEN=your-token-here
```

with:

```dotenv
DISCORD_TOKEN=your-real-token
```

No quotes, no spaces, one line. Save with **Ctrl + S**.

Optional, for instant slash commands and first-run defaults:

```dotenv
GUILD_ID=your-server-id
# Optional defaults for a fresh setup:
TICKET_CATEGORY_ID=
TICKET_LOG_CHANNEL_ID=
TICKET_SUPPORT_ROLE_IDS=
```

If you set `GUILD_ID`, commands appear immediately. Otherwise they are global
and may take up to an hour.

### 5. Start the bot

Double-click **`run.bat`**.

Expected output:

```
INFO     tickets: Synced 1 commands to guild 123456789 (immediate).
INFO     tickets: Cleared global command copies; guild commands remain active.
INFO     tickets: Logged in as Ticket Bot#1234, serving 1 guild(s).
```

### 6. Configure the server

Create these in Discord first:

- a **category** for tickets (e.g. `Tickets`)
- a **channel** for the panel (e.g. `#create-ticket`)
- a **channel** for transcripts (e.g. `#ticket-logs`)
- a **role** for your support team (e.g. `Support`) — a normal role you assign to
  staff, **not** the bot's own auto-created role

Then, in any channel, run (you need **Manage Server**):

```
/ticket setup category:#Tickets panel:#create-ticket log:#ticket-logs support:@Support
```

`/ticket setup` saves the configuration and **posts the panel** in
`#create-ticket`. Replace the values with your own channels/role.

After that:

```
/ticket config
```

to confirm everything is set.

### 7. Permissions that matter

The bot needs these **server-level** permissions:

- **Manage Channels** — create and delete tickets.
- **Manage Roles** — set channel overwrites for the owner and support roles.
- **Manage Messages** — grant it to support roles and delete old panels.
- **View Channels**, **Send Messages**, **Read Message History**, **Embed Links**, **Attach Files** — run the ticket channels.

The bot can only grant permissions in the new ticket channel that it already has in the **parent category**. If your ticket category is private, add the bot's role to it with **View Channel**, **Manage Channels** and **Manage Messages**, or ticket creation fails with `50013`.

> **Why ticket creation used to fail with `50013`:** the bot was adding **Manage Roles (Manage Permissions)** to its own overwrite on the new channel. Discord only allows setting `MANAGE_ROLES` in a channel overwrite at creation time for **Administrators**, so a normal bot gets `403 Missing Permissions` even when it has every other permission. The bot no longer grants Manage Roles in ticket channels; its guild-level **Manage Roles** is enough to add and remove ticket members. If a guild still rejects the full overwrite, the bot automatically retries with a minimal overwrite.

Role position does not affect ticket channels, but keep the bot's role near the top so its permissions are easy to see.

Run `/ticket diagnose` any time to check all of this automatically.

### 8. Test it

1. Open `#create-ticket` and pick a category from the panel.
2. A private channel like `#support-0001` appears.
3. Post a message; a staff member can press **Claim**.
4. Press **Close**, optionally type a reason.
5. Check `#ticket-logs` for the HTML transcript.

The ticket channel is deleted a few seconds after closing.

---

## Commands

All commands are under `/ticket`.

### Setup

| Command | Description |
| --- | --- |
| `/ticket setup category: panel: log: support:` | One-step setup; posts the panel. |
| `/ticket panel channel:` | Post or refresh the ticket panel. |
| `/ticket category category:` | Change where ticket channels are created. |
| `/ticket log channel:` | Set (or clear) the transcript log channel. |
| `/ticket support add role:` | Add a role to the support team. |
| `/ticket support remove role:` | Remove a support role. |
| `/ticket support list` | Show support roles. |
| `/ticket type add label: [description:] [emoji:] [category:] [ping_role:]` | Add a panel category. |
| `/ticket type remove key:` | Remove a panel category (autocomplete). |
| `/ticket type list` | Show all categories and their keys. |
| `/ticket toggle enabled:` | Enable/disable opening new tickets. |
| `/ticket max amount:` | Max open tickets per member (1–25). |
| `/ticket config` | Show the current configuration. |
| `/ticket stats` | Show open/claimed/total ticket counts. |
| `/ticket diagnose` | Check every permission, the category, role hierarchy and channels, and list what to fix. |
| `/ticket reset confirm:true` | Reset the server's configuration (channels are not deleted). |

### Inside a ticket

| Command | Description |
| --- | --- |
| `/ticket close [reason:]` | Close the current ticket. |
| `/ticket claim` | Claim the current ticket. |
| `/ticket add user:` | Let another member see the ticket. |
| `/ticket remove user:` | Remove a member's access. |
| `/ticket rename name:` | Rename the ticket channel. |

Ticket owners can close their own ticket. Support roles, members with
**Manage Channels**, and members with **Manage Server** can claim, add, remove,
rename and close any ticket.

---

## How a ticket works

1. Member selects a category on the panel.
2. A popup asks **"What is the issue?"**; the member types their problem and
   submits it.
3. The bot creates a private text channel named `<key>-0001` under your ticket
   category.
4. Permission overwrites:
   - `@everyone`: **deny View Channel**
   - ticket owner: view, send, read history, attach files, embed links
   - support roles: the same plus **Manage Messages**
   - bot: the same plus **Manage Channels**, **Manage Messages**
     (never **Manage Roles** — Discord rejects that at creation time)
5. The bot posts a ticket embed containing the reported **Issue**, with
   **Claim**, **Add member** and **Close** buttons, and pings the owner (and the
   type's ping role, if configured).
6. On close, the bot:
   - builds an HTML transcript of up to 1500 messages,
   - posts it with a summary embed to the log channel,
   - DMs the transcript to the owner if the log channel fails or is not set,
   - tells you in the ephemeral confirmation where the transcript went,
   - deletes the ticket channel 5 seconds later (in the background, so the
     confirmation always arrives first).

## Transcripts

Transcripts are self-contained `.html` files with attachments linked. Example
name: `ticket-0001.html`. They are safe to open in a browser and easy to store
or attach to a report.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Can't create tickets / permission error | Run `/ticket diagnose` — it lists the exact missing permissions, category problems and role-hierarchy issues. |
| `/ticket` doesn't appear | Set `GUILD_ID` in `.env` and restart. Re-invite with the `applications.commands` scope if it still doesn't show. |
| "Could not open a ticket" / Forbidden `50013` | Make sure the bot's role can **View Channel** and **Manage Channels** in the ticket category. The bot already avoids the `MANAGE_ROLES`-in-overwrite trap described in section 7. |
| "The configured ticket category no longer exists" | Run `/ticket category category:#...` again. |
| Support role can't see tickets | Add it with `/ticket support add role:@Support`. It must be a normal role that staff can have — not the bot's own managed role. |
| Can't add a member to a ticket | The bot needs **Manage Roles**. Also make sure the member is not a bot. |
| Panel select says the category no longer exists | The category was removed; it may have old tickets. Run `/ticket type list` and `/ticket panel` again. |
| Claim does nothing / "This interaction failed" | Fixed in the current code: the buttons used to be built before the event loop started, so discord.py 2.7 silently discarded every click. They are now built in `setup_hook()`. Restart the bot and try again. If it still fails, `logs/ticket-bot.log` shows whether the click arrived and the traceback if one happened. |
| Only staff can claim | Give the person the support role (`/ticket support add role:@Support`) or **Manage Channels**. A regular member keeping their own ticket open does not need to claim it. |
| Closing says "cannot delete the channel" | Give the bot **Manage Channels**. |
| Close button shows "Unknown Message" / nothing happens | Fixed: the channel used to be deleted before the confirmation was sent. The deletion now runs in a background task, and an expired interaction is caught instead of raising. Restart the bot. |
| No transcript in the log channel | Check the bot can **View Channel**, **Send Messages** and **Attach Files** in the log channel. The close confirmation tells you when it failed, and the transcript is DMed to the owner as a fallback. `Could not deliver the transcript to the log channel: 403 ...` in the log confirms it is a permission problem. |
| Duplicate `/ticket` commands | Global copies are cleared on startup after a successful guild sync. Restart once and press **Ctrl + R** in Discord. |
| Console shows `serving 1 guild(s)` but commands 403 | The bot isn't in `GUILD_ID`'s server. Invite it there or fix `GUILD_ID`. |
| `.env still contains the placeholder token` | Open `.env` and paste the real token. |

## Logs

Every run writes to `logs/ticket-bot.log` (rotated at 1 MB, 3 backups) as well as
the console. When something seems to be ignored, check that file first:

- `Claim attempt: ...` confirms the button click reached the bot.
- `Component interaction 'ticket:claim' ...` is logged for every button/select click.
- `Persistent components ready: [...]` confirms the panel and ticket buttons were registered.
- `Ticket component failed` is followed by the traceback that explains a crash.
- `create_text_channel forbidden ... (code ...)` explains a failed ticket.

Set `LOG_LEVEL=DEBUG` in `.env` for more detail.

## Security

- `.env` is ignored by `.gitignore`; never share or commit it.
- If the token leaks, open **Bot → Reset Token** immediately.
- The bot only needs the permissions listed above. Do not give it
  **Administrator**.
- Anyone with the bot token can control the bot; treat it like a password.

## Re-running / stopping

- Start: double-click `run.bat`.
- Stop: press **Ctrl + C** in the console (answer `Y` at the batch prompt).
- Update code: stop the bot, replace `bot.py`, start again. Settings in
  `data/tickets.json` are preserved.
