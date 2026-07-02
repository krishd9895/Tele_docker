"""
DuckDNS Service — auto-update your DuckDNS subdomain to the current public IP.

- update_ip()        : fetch public IP and push to DuckDNS API
- get_public_ip()    : just returns the current public IP string
- start_auto_updater(): background loop, updates every DUCKDNS_UPDATE_INTERVAL seconds
"""

import asyncio
import logging
import aiohttp
from datetime import datetime, timezone

from config.settings import runtime_settings

logger = logging.getLogger(__name__)

DUCKDNS_UPDATE_URL = "https://www.duckdns.org/update"


class DuckDNSService:
    def __init__(self):
        self._last_ip: str = ""
        self._last_updated: str = ""
        self._task: asyncio.Task | None = None

    async def get_public_ip(self) -> str | None:
        """Fetches current public IP from multiple fallback providers."""
        providers = [
            "https://api.ipify.org",
            "https://icanhazip.com",
            "https://ifconfig.me/ip",
        ]
        async with aiohttp.ClientSession() as session:
            for url in providers:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                        ip = (await resp.text()).strip()
                        if ip:
                            return ip
                except Exception:
                    continue
        return None

    async def update_ip(self) -> tuple[bool, str]:
        """
        Push current public IP to DuckDNS.
        Returns (True, ip) on success, (False, error) on failure.
        """
        token = runtime_settings.DUCKDNS_TOKEN
        domain = runtime_settings.DUCKDNS_DOMAIN

        if not token or not domain:
            return False, "DUCKDNS_TOKEN or DUCKDNS_DOMAIN not set in .env"

        ip = await self.get_public_ip()
        if not ip:
            return False, "Could not determine public IP address."

        # DuckDNS domain should be just the subdomain, no .duckdns.org suffix
        subdomain = domain.replace(".duckdns.org", "").strip()

        params = {
            "domains": subdomain,
            "token":   token,
            "ip":      ip,
            "verbose": "true",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    DUCKDNS_UPDATE_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    body = (await resp.text()).strip()
                    # DuckDNS returns "OK" or "KO" on first line
                    if body.upper().startswith("OK"):
                        self._last_ip = ip
                        self._last_updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        logger.info(f"DuckDNS updated: {subdomain}.duckdns.org → {ip}")
                        return True, ip
                    else:
                        logger.warning(f"DuckDNS returned: {body}")
                        return False, f"DuckDNS API error: {body}"
        except Exception as e:
            return False, str(e)

    def get_status(self) -> dict:
        domain = runtime_settings.DUCKDNS_DOMAIN or "not configured"
        return {
            "domain":       domain if domain.endswith(".duckdns.org") else f"{domain}.duckdns.org",
            "last_ip":      self._last_ip or "unknown",
            "last_updated": self._last_updated or "never",
            "auto_update":  self._task is not None and not self._task.done(),
        }

    async def start_auto_updater(self, interval_seconds: int = 300):
        """
        Runs update_ip() every `interval_seconds`. Skips if IP hasn't changed.
        Call this once at bot startup.
        """
        if self._task and not self._task.done():
            return  # already running

        async def _loop():
            logger.info(f"DuckDNS auto-updater started (every {interval_seconds}s)")
            while True:
                try:
                    ip = await self.get_public_ip()
                    if ip and ip != self._last_ip:
                        success, result = await self.update_ip()
                        if success:
                            logger.info(f"DuckDNS IP changed: {self._last_ip} → {result}")
                        else:
                            logger.warning(f"DuckDNS update failed: {result}")
                except Exception as e:
                    logger.error(f"DuckDNS auto-updater error: {e}")
                await asyncio.sleep(interval_seconds)

        self._task = asyncio.create_task(_loop())


duckdns_service = DuckDNSService()
