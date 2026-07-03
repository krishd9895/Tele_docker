"""
/help_guide  — paginated, detailed help for all newly added features.

Sections navigated via inline buttons (no typing needed on mobile):
  1. 🔐 2FA Authentication
  2. 🐙 Git Operations
  3. 🐳 Compose Stack Controls
  4. 🌐 Tunnels (cloudflared)
  5. 🦆 DuckDNS
  6. ⚙️ .env Settings
  7. 🖥️ Host Bridge (/host)
"""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from utils.msg_cleaner import delete_command

help_router = Router()

SECTIONS = [
    (
        "2fa",
        "🔐 2FA Auth",
        """🔐 <b>2FA Authentication</b>

Some commands are protected by Google Authenticator (TOTP).
Verify once and get <b>2 hours</b> of elevated access.

<b>Protected commands:</b>
<code>/shell</code>, <code>/compose_up</code>, <code>/compose_down</code>, <code>/host</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/verify &lt;code&gt;</b>
Authenticate with your 6-digit Google Authenticator code.
Your message is <b>deleted immediately</b> for security.

<i>Example:</i> <code>/verify 482910</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/2fa_status</b>
Shows time remaining on your current session.

<b>/lock</b>
Revoke your 2FA session immediately.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>First-time setup:</b>
1. Open Google Authenticator on your phone
2. Add account → Enter a setup key manually
3. Account name: <code>TeleDocker</code>
4. Key: <i>(your TOTP_SECRET value from .env)</i>
5. Type: Time-based → Done"""
    ),
    (
        "git",
        "🐙 Git Ops",
        """🐙 <b>Git Operations</b>

Clone and update repositories on your WSL machine
from Telegram — no terminal needed.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/gitclone &lt;url&gt; [path]</b>
Clone a Git repository to your WSL machine.

Path is optional — defaults to <code>data/workspaces/&lt;repo-name&gt;</code>

<i>Examples:</i>
<code>/gitclone https://github.com/user/myapp</code>
<code>/gitclone https://github.com/user/myapp /home/d/myapp</code>

• Public repos — works directly
• Private repos — bot asks for your GitHub PAT token
  (deleted immediately after use)

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/gitpull</b>
Pull latest changes from origin.

<b>No argument:</b> Shows all repos as tap-able buttons.
Scans both <code>data/workspaces/</code> (container) and
<code>GIT_SCAN_PATHS</code> on your WSL host via SSH.

Each repo shows a 🐳 (container) or 🖥️ (host) badge.

<b>With path:</b> <code>/gitpull /home/d/myapp</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>GIT_SCAN_PATHS in .env</b>
Comma-separated WSL paths the bot scans for git repos:
<code>GIT_SCAN_PATHS=/home/d</code>
<code>GIT_SCAN_PATHS=/home/d,/home/d/bots</code>"""
    ),
    (
        "compose",
        "🐳 Compose",
        """🐳 <b>Docker Compose Stack Controls</b>

Bring compose stacks up and down from your phone.
Both commands <b>require 2FA</b> — run /verify first.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/compose_up</b>
Runs <code>docker compose up -d</code> on a project.

<b>No argument:</b> Shows all detected compose projects
as tap-able buttons. Scans both container workspace
and WSL host paths (via <code>GIT_SCAN_PATHS</code>).

Each project shows a 🐳 (container) or 🖥️ (host) badge.

<b>With path:</b> <code>/compose_up /home/d/myapp</code>

Auto-detects: <code>docker-compose.yml</code> / <code>compose.yaml</code> etc.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/compose_down</b>
Runs <code>docker compose down</code> on a project.

Same usage — button picker or direct path.
<code>/compose_down /home/d/myapp</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Host projects:</b>
For projects on your WSL host (not inside the container),
the bot runs <code>docker compose</code> via SSH automatically."""
    ),
    (
        "tunnels",
        "🌐 Tunnels",
        """🌐 <b>Public Tunnel Management</b>

Expose any local port as a public HTTPS URL instantly.
Uses <b>cloudflared</b> (Cloudflare's free tunnel tool).
No account, no config, no open ports needed.
All commands <b>require 2FA</b>.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>How it works</b>

cloudflared creates a secure outbound tunnel from your
WSL host to Cloudflare's network. You get a public URL
like <code>https://abc-123.trycloudflare.com</code> instantly.

No account needed. Free. Works behind any firewall or NAT.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/expose</b>
Full wizard — installs cloudflared automatically on first use:

<b>Step 1</b> — Enter local port or URL:
<code>8080</code>  or  <code>http://localhost:3000</code>

<b>Step 2</b> — Health check:
Bot pings your service. Aborts if unreachable.

<b>Step 3</b> — Basic Auth (tap button):
• 🔒 Yes → enter username + password (password deleted immediately)
• 🔓 No → open access

<b>Step 4</b> — Tunnel launches.
Bot returns public HTTPS URL + tunnel ID.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/expose_setup</b>
Manually install cloudflared on your WSL host.
Not needed — /expose auto-installs on first use.
Use this only if auto-install fails.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/tunnel_status</b>
Shows all active tunnels + DuckDNS IP status.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/revoke &lt;id&gt;</b>
Kill a tunnel — URL goes dead immediately.
No arg → shows active tunnels as tap-able buttons.
<code>/revoke a1b2c3</code>"""
    ),
    (
        "duckdns",
        "🦆 DuckDNS",
        """🦆 <b>DuckDNS Auto IP Updater</b>

Keeps your <code>yourname.duckdns.org</code> subdomain
always pointing at your current public IP.
Fully configurable from the bot — no manual .env editing needed.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/duckdns</b> — control panel:

• <b>🔍 Status</b> — domain, registered IP, live IP,
  mismatch warning if they differ
• <b>🔄 Update IP Now</b> — force push immediately
• <b>▶/⏹ Auto-Update</b> — start or stop background updater
• <b>⚙️ Configure</b> — set token and domain interactively

━━━━━━━━━━━━━━━━━━━━━━━━
<b>First-time setup from phone:</b>

1. Open <code>duckdns.org</code> and log in
2. Copy your token (top of the page)
3. Open /duckdns → tap ⚙️ Configure
4. Paste token → enter subdomain (e.g. <code>kyone</code>)
5. Bot tests the connection, saves to <code>.env</code>,
   and starts auto-update — all automatically

✅ No manual file editing needed.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Auto-update interval</b>
Default: every 5 minutes.
Change with: <code>/setenv DUCKDNS_UPDATE_INTERVAL 600</code>"""
    ),
    (
        "env",
        "⚙️ .env Settings",
        """⚙️ <b>Settings Management</b>

View and update bot configuration from Telegram.
Both commands <b>require 2FA</b>.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>How it works (secure design)</b>

<code>.env</code> is mounted <b>read-only</b> — the bot can
never modify tokens or passwords.

Mutable settings (DuckDNS, scan paths, timeouts)
are stored in:
• ☁️ <b>MongoDB</b> — if MONGO_URI is set
• 📄 <b>data/settings.json</b> — always available as fallback

On restart, .env loads first, then the store overlays.
So settings saved via /setenv survive restarts automatically.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/showenv</b>
Shows all settings with source badges:
• 🔒 = from .env (read-only)
• ☁️ = saved in MongoDB
• 📄 = saved in settings.json
Sensitive values are always masked.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/setenv &lt;KEY&gt; &lt;value&gt;</b>
Save a mutable setting. Applied immediately to runtime.
No restart needed for most settings.

<i>Examples:</i>
<code>/setenv GIT_SCAN_PATHS /home/d</code>
<code>/setenv DUCKDNS_UPDATE_INTERVAL 600</code>
<code>/setenv DEPLOYMENT_TIMEOUT 900</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Blocked (sensitive — .env only):</b>
<code>TELEGRAM_BOT_TOKEN</code>, <code>TOTP_SECRET</code>,
<code>HOST_SSH_PASSWORD</code>, <code>DUCKDNS_TOKEN</code>,
<code>ALLOWED_USER_ID</code>, <code>MONGO_URI</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>MongoDB (optional)</b>
Set <code>MONGO_URI</code> in .env for cloud-synced settings.
Without it, <code>data/settings.json</code> is used automatically."""
    ),
    (
        "host",
        "🖥️ Host Bridge",
        """🖥️ <b>Host Bridge — /host command</b>

Run shell commands directly on your <b>WSL Ubuntu host</b>
from inside the Docker container, via SSH loopback.

<b>Requires 2FA</b> — run /verify first.
Hidden command — not in the /help menu.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Usage:</b> <code>/host &lt;any shell command&gt;</code>

<i>Examples:</i>
<code>/host whoami</code>
<code>/host ls /home/d</code>
<code>/host df -h</code>
<code>/host free -h</code>
<code>/host systemctl status nginx</code>
<code>/host docker ps</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/host vs /shell:</b>

<code>/shell</code> → inside the Docker container
  Filesystem: <code>/app/</code> only

<code>/host</code> → your WSL Ubuntu machine
  Filesystem: <code>/home/d/</code> and everything else
  Can run systemctl, see all processes, etc.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Requirements:</b>
<code>sudo service ssh start</code> on WSL

In <code>.env</code>:
<code>HOST_SSH_USER=d</code>
<code>HOST_SSH_PASSWORD=yourpass</code>

Output trimmed to 3800 chars if very long."""
    ),
]

_SECTION_MAP = {s[0]: s for s in SECTIONS}


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for sid, label, _ in SECTIONS:
        row.append(InlineKeyboardButton(text=label, callback_data=f"hg:{sid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _section_keyboard(current_id: str) -> InlineKeyboardMarkup:
    ids = [s[0] for s in SECTIONS]
    idx = ids.index(current_id)
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(
            text=f"◀ {SECTIONS[idx-1][1]}", callback_data=f"hg:{ids[idx-1]}"
        ))
    if idx < len(ids) - 1:
        nav.append(InlineKeyboardButton(
            text=f"{SECTIONS[idx+1][1]} ▶", callback_data=f"hg:{ids[idx+1]}"
        ))
    buttons = []
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="📋 Back to Menu", callback_data="hg:__menu__")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@help_router.message(Command("help_guide"))
async def cmd_help_guide(message: Message):
    await delete_command(message)
    await message.answer(
        "📖 <b>Feature Guide</b>\n\n"
        "Select a topic to learn how to use it.\n"
        "All sections use buttons — no typing needed.\n",
        parse_mode="HTML",
        reply_markup=_main_menu_keyboard()
    )


@help_router.callback_query(F.data.startswith("hg:"))
async def cb_help_section(call: CallbackQuery):
    section_id = call.data.split("hg:", 1)[1]
    await call.answer()

    if section_id == "__menu__":
        await call.message.edit_text(
            "📖 <b>Feature Guide</b>\n\n"
            "Select a topic to learn how to use it.\n"
            "All sections use buttons — no typing needed.\n",
            parse_mode="HTML",
            reply_markup=_main_menu_keyboard()
        )
        return

    section = _SECTION_MAP.get(section_id)
    if not section:
        await call.message.answer("⚠️ Section not found.")
        return

    _, _, content = section
    await call.message.edit_text(
        content,
        parse_mode="HTML",
        reply_markup=_section_keyboard(section_id),
        disable_web_page_preview=True
    )
