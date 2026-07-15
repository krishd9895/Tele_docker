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
from config.settings import runtime_settings

help_router = Router()


def _get_2fa_section_content() -> str:
    duration_hours = runtime_settings.TOTP_SESSION_DURATION / 3600
    return f"""🔐 <b>2FA Authentication</b>

Some commands are protected by Google Authenticator (TOTP).
Verify once and get <b>{duration_hours} hours</b> of elevated access.

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


def _get_sections() -> list[tuple[str, str, str]]:
    return [
        (
            "2fa",
            "🔐 2FA Auth",
            _get_2fa_section_content()
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

Two modes — both use cloudflared, no port forwarding needed.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/expose</b>  — Temporary (no account)

Random URL like <code>abc-xyz.trycloudflare.com</code>
Changes every time you run it. Good for testing.

Wizard steps:
1. Enter port: <code>8080</code>
2. Health check (auto)
3. Basic auth? Yes/No
4. URL ready

━━━━━━━━━━━━━━━━━━━━━━━━
━━━━━━━━━━━━━━━━━━━━━━━━
<b>/expose_perm</b>  — Permanent (free CF account)

Fixed URL like <code>myapp.yourdomain.com</code>
Never changes. Survives restarts.

<b>One-time setup (from phone):</b>
1. Tap /expose_perm → 📋 How to get a token
2. Open <code>cloudflare.com</code> → Sign up free
3. Dashboard → Zero Trust → Networks → Tunnels
4. Create tunnel → Cloudflared → name it → Save
5. On the install page — copy the long token
   after the word <code>install</code> (starts with <code>eyJ</code>)
   ⚠️ Don't run that command — bot does it for you
6. Still on same page → Next: Route tunnel
   Add public hostname:
   • Subdomain: <code>app</code>
   • Domain: your domain or <code>workers.dev</code>
   • URL: <code>localhost:8080</code> → Save
7. Back in bot → /expose_perm → Enter token → paste it
8. ✅ Permanent URL active — saved for future restarts

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/tunnel_status</b>
Active tunnels (⏱ temp / ♾️ permanent) + DuckDNS IP

<b>/revoke &lt;id&gt;</b> — stop one tunnel
<b>/revoke_all</b> — kill everything (use after restart to
clean up old orphaned processes)"""
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
            """🖥️ <b>Host Bridge — /host & /host_cd commands</b>

Run shell commands directly on your <b>WSL Ubuntu host</b>
from inside the Docker container, via SSH loopback.

<b>Requires 2FA</b> — run /verify first.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/host &lt;any shell command&gt;</b>
Run a command on the host (always starts in home directory).

<i>Examples:</i>
<code>/host whoami</code>
<code>/host ls /home/d</code>
<code>/host df -h</code>
<code>/host free -h</code>
<code>/host systemctl status nginx</code>
<code>/host docker ps</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/host_cd &lt;path&gt; &lt;command&gt;</b>
Run a command in a specific directory on the host.

<i>Examples:</i>
<code>/host_cd /home/d/projects ls -la</code>
<code>/host_cd /var/log tail -n 50 syslog</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/host vs /shell:</b>

<code>/shell</code> → inside the Docker container
  Filesystem: <code>/app/</code> only

<code>/host</code> / <code>/host_cd</code> → your WSL Ubuntu machine
  Filesystem: <code>/home/d/</code> and everything else
  Can run systemctl, see all processes, etc.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Requirements:</b>
<code>sudo service ssh start</code> on WSL

In <code>.env</code>:
<code>HOST_SSH_USER=Ubuntu</code>
<code>HOST_SSH_PASSWORD=yourpass</code>

Output trimmed to 3800 chars if very long."""
        ),
        (
            "pydeploy",
            "🐍 Py Deploy",
            """🐍 <b>Python App Deployments</b>

Deploy a plain Python project (no Docker) straight from Telegram —
it runs 24/7 on your WSL host with its own virtual environment and
restarts itself automatically if it ever crashes.

<b>Requires 2FA</b> — run /verify first.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/pydeploy &lt;github_url&gt; [entry_file.py]</b>
Deploy directly from a GitHub URL.

• Public repos — works directly
• Private repos — embed a token in the URL yourself, e.g.
  <code>https://TOKEN@github.com/user/repo.git</code>
  (never echoed back in chat)
• Entry file is auto-detected (<code>main.py</code>, <code>app.py</code>,
  <code>bot.py</code>, <code>run.py</code>, <code>server.py</code>) or pass
  it explicitly as the second argument.

<i>Example:</i>
<code>/pydeploy https://github.com/user/mybot</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/pydeploy</b> (no arguments)
Prompts you to send a GitHub URL <b>or</b> upload a
<code>.zip</code>/<code>.rar</code> archive of your project instead.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Projects with a Dockerfile/compose file:</b>
Not rejected — you'll be asked to either use /deploy or /compose_up
for full Docker support, or continue right there as a plain Python
app by optionally supplying a custom <b>build command</b>, <b>run
command</b>, and <code>.env</code> content. Skip any of them; skipping
the run command falls back to auto-detecting the entry point.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/pyps</b>
Lists every Python deployment with its status. Tap a deployment to:
▶ Start · ⏹ Stop · 🔄 Restart · 📄 View logs

Deployments left running are auto-restarted if they crash. Stopping
one via ⏹ turns auto-restart off for it until you tap ▶ again.

<b>Updating code:</b>
⬇️ <b>Git Pull Update</b> — pulls the latest commit (git-sourced deployments
only), then rebuilds and restarts with your existing build/run
command and <code>.env</code>.
📤 <b>Upload New Version</b> — send a fresh <code>.zip</code>/<code>.rar</code> to fully replace the
code (keeps the virtual environment, logs, and <code>.env</code>), then rebuilds
and restarts.

<b>Removing a deployment:</b>
🗑️ <b>Delete</b> — asks for confirmation, then stops the process and
permanently removes its files, virtual environment, and logs from
the host.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Under the hood:</b>
Each deployment gets its own folder + virtual environment on the
WSL host under <code>PY_DEPLOY_ROOT</code> (default
<code>~/py_deployments</code>, configurable in <code>.env</code>)."""
        ),
    ]


def _get_section_map() -> dict[str, tuple[str, str, str]]:
    return {s[0]: s for s in _get_sections()}


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for sid, label, _ in _get_sections():
        row.append(InlineKeyboardButton(text=label, callback_data=f"hg:{sid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _section_keyboard(current_id: str) -> InlineKeyboardMarkup:
    sections = _get_sections()
    ids = [s[0] for s in sections]
    idx = ids.index(current_id)
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(
            text=f"◀ {sections[idx-1][1]}", callback_data=f"hg:{ids[idx-1]}"
        ))
    if idx < len(ids) - 1:
        nav.append(InlineKeyboardButton(
            text=f"{sections[idx+1][1]} ▶", callback_data=f"hg:{ids[idx+1]}"
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

    section_map = _get_section_map()
    section = section_map.get(section_id)
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