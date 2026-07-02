"""
/help_guide  — paginated, detailed help for all newly added features.

Sections navigated via inline buttons (no typing needed on mobile):
  1. 🔐 2FA Authentication
  2. 🐙 Git Operations
  3. 🐳 Compose Stack Controls
  4. 🌐 Zrok Tunnels
  5. 🦆 DuckDNS
  6. 🖥️ Host Bridge (/host)
"""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from utils.msg_cleaner import delete_command

help_router = Router()

# ── Section definitions ───────────────────────────────────────────────────────
# Each entry: (callback_id, menu_label, full_text)

SECTIONS = [
    (
        "2fa",
        "🔐 2FA Auth",
        """🔐 <b>2FA Authentication</b>

Some commands are protected by Google Authenticator (TOTP).
You must verify once every <b>2 hours</b> before using them.

<b>Protected commands:</b>
<code>/shell</code>, <code>/compose_up</code>, <code>/compose_down</code>, <code>/host</code>, <code>/expose</code>, <code>/revoke</code>, <code>/tunnel_status</code>, <code>/zrok_setup</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/verify &lt;code&gt;</b>
Authenticate using your 6-digit Google Authenticator code.
Your message is <b>deleted immediately</b> for security.

<i>Example:</i>
<code>/verify 482910</code>

After success → elevated access for 2 hours.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/2fa_status</b>
Shows how much time is left on your current session.

<i>Example output:</i>
🟢 Session Active — Time remaining: <code>1h 42m 8s</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/lock</b>
Manually revoke your 2FA session immediately.
Use this when you're done with elevated commands.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>First-time setup:</b>
1. Open Google Authenticator on your phone
2. Add account → Enter a setup key manually
3. Account name: <code>TeleDocker</code>
4. Key: <i>(the TOTP_SECRET value from your .env)</i>
5. Type: Time-based → Done"""
    ),
    (
        "git",
        "🐙 Git Ops",
        """🐙 <b>Git Operations</b>

Clone and update repositories directly on your WSL machine
from Telegram — no terminal needed.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/gitclone &lt;url&gt; [path]</b>
Clone a Git repository to your WSL machine.

<code>path</code> is optional — if omitted, clones to
<code>data/workspaces/&lt;repo-name&gt;</code>

<i>Examples:</i>
<code>/gitclone https://github.com/user/myapp</code>
<code>/gitclone https://github.com/user/myapp /home/user/projects/myapp</code>

Supports:
• Public repos — works directly
• Private repos — bot will ask for your GitHub PAT token
  (the token message is deleted immediately after use)

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/gitpull</b>
Pull latest changes from origin.

<b>No argument:</b> Shows a list of all repos in
<code>data/workspaces/</code> as tap-able buttons.
Tap one → bot pulls it immediately.

<b>With path:</b> Pull a specific repo by path.
<code>/gitpull /home/user/projects/myapp</code>

<i>What it shows per repo:</i>
• Repo name
• Remote URL (tokens stripped from display)
• Local path"""
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

<b>No argument:</b> Scans <code>data/workspaces/</code> for
folders containing a compose file and shows them as
tap-able buttons. Tap one → stack comes up.

<b>With path:</b> Target a specific folder directly.
<code>/compose_up /home/user/projects/myapp</code>

Auto-detects the compose file name:
<code>docker-compose.yml</code> / <code>docker-compose.yaml</code> /
<code>compose.yaml</code> / <code>compose.yml</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/compose_down</b>
Runs <code>docker compose down</code> on a project.

Same usage as /compose_up — button picker or direct path.
<code>/compose_down /home/user/projects/myapp</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Output:</b>
Both commands show the full compose output in the reply
(last 3000 characters if very long)."""
    ),
    (
        "zrok",
        "🌐 Zrok Tunnels",
        """🌐 <b>Zrok2 Tunnel Management</b>

Expose any local port as a public HTTPS URL.
Uses your self-hosted <b>zrok2</b> instance running as
systemd services on WSL Ubuntu.
All tunnel commands <b>require 2FA</b>.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Prerequisites (one-time server setup)</b>

zrok2 runs as Linux systemd services — not Docker.
Install on your WSL host:
<code>sudo apt install zrok2 zrok2-controller zrok2-frontend</code>

Bootstrap (generates admin token):
<code>export ZROK2_ADMIN_TOKEN="$(head -c24 /dev/urandom | base64 -w0)"
sudo -E /usr/share/zrok/nfpm/zrok2-bootstrap.bash</code>

Save the <code>ZROK2_ADMIN_TOKEN</code> value into .env:
<code>ZROK_PRIVATE_TOKEN=that_value</code>
<code>ZROK_API_ENDPOINT=http://127.0.0.1:18080</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/zrok_install_local</b>  ← if apt fails
1. Copy <code>zrok_x.x.x_linux_amd64.tar.gz</code>
   into <code>/home/d/Tele_docker/</code> via SSH/WinSCP
2. Run /zrok_install_local — bot installs automatically

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/zrok_setup</b>  — button panel:
• <b>🔍 Check Status</b> — binary installed? enrolled?
  Also shows zrok2-controller systemd service state
• <b>📥 Install zrok</b> — tries apt, then local file
• <b>🔑 Enroll Account</b> — auto-creates account using
  ZROK_PRIVATE_TOKEN (admin token) + email/password
• <b>🦆 Update DuckDNS</b> — push current IP

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/expose</b> — create a public tunnel:
Step 1 → enter port: <code>8080</code>
Step 2 → health check (bot pings it)
Step 3 → basic auth yes/no
Step 4 → tunnel launches, public URL returned

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/tunnel_status</b> — all active tunnels + DuckDNS

<b>/revoke &lt;id&gt;</b> — kill tunnel instantly (URL → 404)
No arg → tap-able button list"""
    ),
    (
        "duckdns",
        "🦆 DuckDNS",
        """🦆 <b>DuckDNS Auto IP Updater</b>

Keeps your <code>yourname.duckdns.org</code> subdomain
always pointing at your current public IP address.
Runs silently in the background — no interaction needed.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Setup (one-time .env edit):</b>

<code>DUCKDNS_TOKEN=your-token-here</code>
<code>DUCKDNS_DOMAIN=yourname</code>
<code>DUCKDNS_UPDATE_INTERVAL=300</code>

• Get your token at <b>duckdns.org</b> after logging in
• <code>DUCKDNS_DOMAIN</code> = just the subdomain part
  e.g. <code>myserver</code> not <code>myserver.duckdns.org</code>
• <code>UPDATE_INTERVAL</code> = seconds between checks (default 300 = 5 min)
  IP is only pushed to DuckDNS when it actually changes.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>What it does:</b>
Every 5 minutes the bot fetches your public IP from
ipify.org / icanhazip.com (with fallbacks).
If IP changed → pushes update to DuckDNS API.
If IP same → skips silently.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Manual update:</b>
Inside /zrok_setup → tap <b>🦆 Update DuckDNS</b>
to force-push your current IP on demand.

<b>Check status:</b>
<code>/tunnel_status</code> — shows current IP, domain,
last update time, and whether auto-update is running.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>DuckDNS vs zrok — what's the difference?</b>

<code>yourname.duckdns.org</code>
→ Points at your WSL machine's public IP.
  You decide what runs on each port.
  Always-on, no tunnel needed.

<code>abc123.share.zrok.io</code>
→ A temporary HTTPS tunnel to one specific
  local port, created on-demand via /expose.
  Deleted when you /revoke it."""
    ),
    (
        "host",
        "🖥️ Host Bridge",
        """🖥️ <b>Host Bridge — /host command</b>

Run shell commands directly on your <b>WSL Ubuntu host</b>
from inside the Docker container, via SSH loopback.

<b>Requires 2FA</b> — run /verify first.
Not listed in the help menu (hidden command).

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Usage:</b>
<code>/host &lt;any shell command&gt;</code>

<i>Examples:</i>
<code>/host whoami</code>
<code>/host ls /home/youruser</code>
<code>/host df -h</code>
<code>/host free -h</code>
<code>/host systemctl status nginx</code>
<code>/host cat /etc/hosts</code>
<code>/host docker ps</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>/host vs /shell — key difference:</b>

<code>/shell</code> → runs inside the Docker container
  Filesystem: <code>/app/</code> (container only)
  Processes: container's process space

<code>/host</code> → runs on your WSL Ubuntu machine
  Filesystem: <code>/home/youruser/</code> (full host)
  Processes: host services, systemctl, etc.

━━━━━━━━━━━━━━━━━━━━━━━━
<b>Requirements:</b>
SSH must be running on WSL:
<code>sudo service ssh start</code>

Credentials set in <code>.env</code>:
<code>HOST_SSH_USER=youruser</code>
<code>HOST_SSH_PASSWORD=yourpass</code>

Output is trimmed to 3800 chars if very long."""
    ),
]

# Build section index for quick lookup
_SECTION_MAP = {s[0]: s for s in SECTIONS}


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """Two-column grid of section buttons."""
    buttons = []
    row = []
    for i, (sid, label, _) in enumerate(SECTIONS):
        row.append(InlineKeyboardButton(text=label, callback_data=f"hg:{sid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _section_keyboard(current_id: str) -> InlineKeyboardMarkup:
    """Back button + prev/next navigation."""
    ids = [s[0] for s in SECTIONS]
    idx = ids.index(current_id)

    nav = []
    if idx > 0:
        prev_label = SECTIONS[idx - 1][1]
        nav.append(InlineKeyboardButton(text=f"◀ {prev_label}", callback_data=f"hg:{ids[idx-1]}"))
    if idx < len(ids) - 1:
        next_label = SECTIONS[idx + 1][1]
        nav.append(InlineKeyboardButton(text=f"{next_label} ▶", callback_data=f"hg:{ids[idx+1]}"))

    buttons = []
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="📋 Back to Menu", callback_data="hg:__menu__")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Handlers ──────────────────────────────────────────────────────────────────

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
    # Guide stays open — user navigates it interactively, no auto-delete


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
