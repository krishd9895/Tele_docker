"""
Rescue bot — a stripped-down emergency controller that keeps running (or
starts independently) when the main tg-manager-bot container is down.

Commands
────────
  /docker_ps       — all containers, grouped by status (matches main bot style)
  /docker_images   — all images, grouped by usage (matches main bot style)
  /docker_restart  — restart a named container
"""

import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
import docker

token = os.getenv('TELEGRAM_BOT_TOKEN', '')
allowed_id = int(os.getenv('ALLOWED_USER_ID', '0'))

bot = Bot(token=token)
dp = Dispatcher()
client = docker.from_env()

# ── Status group definitions ──────────────────────────────────────────────────
# Ordered so the most actionable states surface first; a fallback bucket
# catches any status Docker may add in future versions.
_CONTAINER_STATUS_GROUPS = [
    ("running",    "🟢 Running"),
    ("restarting", "🔁 Restarting"),
    ("paused",     "🟡 Paused"),
    ("created",    "⚪ Created"),
    ("exited",     "🔴 Exited"),
    ("dead",       "⚫ Dead"),
    ("removing",   "🗑 Removing"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _get_ports(container) -> str:
    """Return a compact port-mapping string, e.g. '8080→80, 443→443'."""
    try:
        ports = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
        mappings = []
        for container_port, bindings in ports.items():
            if not bindings:
                continue
            proto = ""
            if "/udp" in container_port:
                proto = "/udp"
            for b in bindings:
                host_port = b.get("HostPort", "?")
                cport = container_port.split("/")[0]
                mappings.append(f"{host_port}→{cport}{proto}")
        if not mappings:
            return ""
        # Cap at 3 to keep lines short
        shown = mappings[:3]
        suffix = f" +{len(mappings)-3}" if len(mappings) > 3 else ""
        return f" | 🔌 {', '.join(shown)}{suffix}"
    except Exception:
        return ""


def _group_containers_by_status(containers) -> str:
    """
    Group containers by live status into labelled sections.
    Matches the visual style of the main tg-manager-bot /docker_ps output.
    """
    buckets: dict[str, list] = {}
    for c in containers:
        buckets.setdefault(c.status, []).append(c)

    lines: list[str] = []
    seen: set[str] = set()

    def render_bucket(label: str, items: list):
        lines.append(f"<b>{label} ({len(items)})</b>")
        for c in items:
            tag = c.image.tags[0] if c.image.tags else "Untagged"
            short_id = c.short_id
            ports = _get_ports(c)
            lines.append(
                f"• <code>{c.name}</code>"
                f" | <code>{short_id}</code>"
                f" | {tag}"
                f"{ports}"
            )
        lines.append("")

    # Render in the canonical priority order first…
    for status_key, label in _CONTAINER_STATUS_GROUPS:
        items = buckets.get(status_key)
        if items:
            render_bucket(label, items)
            seen.add(status_key)

    # …then any unexpected status Docker may return
    for status_key, items in buckets.items():
        if status_key not in seen:
            render_bucket(f"❔ {status_key.title()}", items)

    return "\n".join(lines).rstrip()


def _group_images(images, in_use_ids: set) -> str:
    """
    Group images into In Use / Unused / Dangling sections.
    Matches the visual style of the main tg-manager-bot /docker_images output.
    """
    in_use, unused, dangling = [], [], []
    for img in images:
        if not img.tags:
            dangling.append(img)
        elif img.id in in_use_ids:
            in_use.append(img)
        else:
            unused.append(img)

    def render_group(label: str, items: list) -> list[str]:
        out = [f"<b>{label} ({len(items)})</b>"]
        for img in items:
            tags = (
                ", ".join(img.tags)
                if img.tags
                else "&lt;none&gt;:&lt;none&gt;"
            )
            size = _format_bytes(img.attrs.get("Size", 0))
            short_id = img.short_id.replace("sha256:", "")
            out.append(f"• <code>{tags}</code> | {short_id} | {size}")
        out.append("")
        return out

    lines: list[str] = []
    if in_use:
        lines += render_group("📌 In Use", in_use)
    if unused:
        lines += render_group("📦 Unused (tagged, no container)", unused)
    if dangling:
        lines += render_group("🗑 Dangling (untagged)", dangling)

    return "\n".join(lines).rstrip()


# ── Handlers ──────────────────────────────────────────────────────────────────

@dp.message(Command("docker_ps"))
async def rescue_ps(message: Message):
    if message.from_user.id != allowed_id:
        return

    try:
        containers = await asyncio.to_thread(client.containers.list, all=True)
    except Exception as e:
        await message.answer(
            f"❌ <b>Could not reach Docker daemon:</b>\n<code>{e}</code>",
            parse_mode="HTML",
        )
        return

    if not containers:
        await message.answer(
            "<b>🚨 Rescue — Container Status</b>\n\n"
            "ℹ️ No containers found on this host.",
            parse_mode="HTML",
        )
        return

    grouped = _group_containers_by_status(containers)
    await message.answer(
        f"<b>🚨 Rescue — Container Status ({len(containers)} total)</b>\n\n{grouped}",
        parse_mode="HTML",
    )


@dp.message(Command("docker_images"))
async def rescue_images(message: Message):
    if message.from_user.id != allowed_id:
        return

    try:
        images = await asyncio.to_thread(client.images.list, all=False)
        containers = await asyncio.to_thread(client.containers.list, all=True)
    except Exception as e:
        await message.answer(
            f"❌ <b>Could not reach Docker daemon:</b>\n<code>{e}</code>",
            parse_mode="HTML",
        )
        return

    if not images:
        await message.answer(
            "<b>🚨 Rescue — Image Registry</b>\n\n"
            "ℹ️ No images present on this host.",
            parse_mode="HTML",
        )
        return

    in_use_ids = {c.image.id for c in containers}
    body = _group_images(images, in_use_ids)

    res = f"<b>🚨 Rescue — Image Registry ({len(images)} total)</b>\n\n{body}"
    if len(res) > 4000:
        res = res[:4000] + "\n\n<i>… truncated</i>"
    await message.answer(res, parse_mode="HTML")


@dp.message(Command("docker_restart"))
async def rescue_restart(message: Message):
    if message.from_user.id != allowed_id:
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "⚠️ Usage: <code>/docker_restart &lt;container_name&gt;</code>",
            parse_mode="HTML",
        )
        return

    name = args[1]
    try:
        c = await asyncio.to_thread(client.containers.get, name)
        await asyncio.to_thread(c.restart)
        await message.answer(
            f"✅ <b>Restarted:</b> <code>{name}</code>",
            parse_mode="HTML",
        )
    except docker.errors.NotFound:
        await message.answer(
            f"❌ Container <code>{name}</code> not found.",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(
            f"❌ <b>Restart failed:</b>\n<code>{e}</code>",
            parse_mode="HTML",
        )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    if not token or allowed_id == 0:
        print("Rescue bot skipped: TELEGRAM_BOT_TOKEN or ALLOWED_USER_ID not set.")
        return

    # Retry loop for startup network failures (e.g. the host's internet is
    # momentarily down when the container first comes up). Unlike the main bot,
    # the rescue bot registers all handlers via module-level @dp decorators, so
    # calling dp.start_polling() again after a network failure is safe — there
    # is no router double-attach problem here.
    attempt = 0
    while True:
        try:
            attempt += 1
            print(f"🚨 Rescue bot starting (attempt {attempt})...")
            await dp.start_polling(bot)
            # start_polling only returns on a clean shutdown (SIGTERM/SIGINT)
            break
        except (KeyboardInterrupt, SystemExit):
            print("Rescue bot shutting down.")
            break
        except Exception as e:
            backoff = min(60, 5 * attempt)
            print(f"Rescue bot startup/polling error (attempt {attempt}): {e}")
            print(f"Retrying in {backoff}s...")
            await asyncio.sleep(backoff)


if __name__ == '__main__':
    asyncio.run(main())
