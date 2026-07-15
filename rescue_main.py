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

# Ordered so the most "actionable" states surface first, with a fallback
# bucket for any status Docker reports that isn't explicitly listed here.
_CONTAINER_STATUS_GROUPS = [
    ("running", "🟢 Running"),
    ("restarting", "🔁 Restarting"),
    ("paused", "🟡 Paused"),
    ("created", "⚪ Created"),
    ("exited", "🔴 Exited"),
    ("dead", "⚫ Dead"),
    ("removing", "🗑 Removing"),
]


def _group_containers_by_status(containers) -> str:
    buckets = {}
    for c in containers:
        buckets.setdefault(c.status, []).append(c)

    lines = []
    seen_statuses = set()

    def render_bucket(label, items):
        lines.append(f"<b>{label} ({len(items)})</b>")
        for c in items:
            tag = c.image.tags[0] if c.image.tags else "Untagged"
            lines.append(f"• <code>{c.name}</code> | {tag} | <code>{c.status}</code>")
        lines.append("")

    for status_key, label in _CONTAINER_STATUS_GROUPS:
        items = buckets.get(status_key)
        if items:
            render_bucket(label, items)
            seen_statuses.add(status_key)

    for status_key, items in buckets.items():
        if status_key not in seen_statuses:
            render_bucket(f"❔ {status_key.title()}", items)

    return "\n".join(lines).rstrip()


def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@dp.message(Command("docker_ps"))
async def rescue_ps(message: Message):
    if message.from_user.id != allowed_id: return
    containers = client.containers.list(all=True)
    if not containers:
        await message.answer("<b>🚨 Rescue Mode Node Tracker</b>\n\nℹ️ No container clusters observed.", parse_mode="HTML")
        return
    grouped = _group_containers_by_status(containers)
    res = f"<b>🚨 Rescue Mode Node Tracker ({len(containers)} total)</b>\n\n{grouped}"
    await message.answer(res, parse_mode="HTML")


@dp.message(Command("docker_images"))
async def rescue_images(message: Message):
    if message.from_user.id != allowed_id: return
    images = client.images.list(all=False)
    containers = client.containers.list(all=True)
    if not images:
        await message.answer("<b>🚨 Rescue Mode Image Tracker</b>\n\nℹ️ No images present.", parse_mode="HTML")
        return

    in_use_ids = {c.image.id for c in containers}
    in_use, unused, dangling = [], [], []
    for img in images:
        if not img.tags:
            dangling.append(img)
        elif img.id in in_use_ids:
            in_use.append(img)
        else:
            unused.append(img)

    def render_group(label, items):
        out = [f"<b>{label} ({len(items)})</b>"]
        for img in items:
            tags = ", ".join(img.tags) if img.tags else "&lt;none&gt;:&lt;none&gt;"
            size = _format_bytes(img.attrs.get("Size", 0))
            short_id = img.short_id.replace("sha256:", "")
            out.append(f"• <code>{tags}</code> | {short_id} | {size}")
        out.append("")
        return out

    lines = []
    if in_use:
        lines += render_group("📌 In Use", in_use)
    if unused:
        lines += render_group("📦 Unused (tagged, no container)", unused)
    if dangling:
        lines += render_group("🗑 Dangling (untagged)", dangling)

    body = "\n".join(lines).rstrip()
    res = f"<b>🚨 Rescue Mode Image Tracker ({len(images)} total)</b>\n\n{body}"
    if len(res) > 4000:
        res = res[:4000] + "\n\n<i>… truncated</i>"
    await message.answer(res, parse_mode="HTML")

@dp.message(Command("docker_restart"))
async def rescue_restart(message: Message):
    if message.from_user.id != allowed_id: return
    args = message.text.split()
    if len(args) > 1:
        try:
            c = client.containers.get(args[1])
            c.restart()
            await message.answer(f"✅ Rescue instruction power-cycled: {args[1]}")
        except Exception as e:
            await message.answer(f"❌ Action failed: {str(e)}")

async def main():
    if not token or allowed_id == 0:
        print("Rescue bot skipped: Missing structural validation variables.")
        return
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())