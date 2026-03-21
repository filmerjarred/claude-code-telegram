"""Supabase JWT authentication for the web interface.

Validates tokens by calling the Supabase Auth REST API,
matching the same pattern GardenFS uses in server.js.
"""

import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx
import structlog

logger = structlog.get_logger()

# Cache validated tokens for 5 minutes (matches GardenFS AUTH_CACHE_TTL)
_AUTH_CACHE_TTL = 5 * 60


@dataclass
class SupabaseUser:
    """Authenticated Supabase user."""

    id: str  # Supabase UUID
    email: str
    name: Optional[str] = None
    avatar_url: Optional[str] = None


class SupabaseAuthValidator:
    """Validates Supabase JWTs by calling the Supabase Auth API.

    Mirrors the GardenFS authenticate() function in server.js:
    creates a temporary client with the token and calls getUser().
    """

    def __init__(self, supabase_url: str, supabase_anon_key: str) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.supabase_anon_key = supabase_anon_key
        self._cache: Dict[str, Tuple[SupabaseUser, float]] = {}
        self._client = httpx.AsyncClient(timeout=10.0)

    async def validate_token(self, token: str) -> Optional[SupabaseUser]:
        """Validate a Supabase JWT and return the user, or None if invalid.

        Calls GET {supabase_url}/auth/v1/user with the token,
        which is what supabase.auth.getUser(token) does under the hood.
        """
        # Check cache
        cached = self._cache.get(token)
        if cached:
            user, expires_at = cached
            if time.monotonic() < expires_at:
                return user
            del self._cache[token]

        # Call Supabase Auth API
        try:
            resp = await self._client.get(
                f"{self.supabase_url}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": self.supabase_anon_key,
                },
            )
            if resp.status_code != 200:
                logger.debug("Supabase token validation failed", status=resp.status_code)
                return None

            data = resp.json()
            user = SupabaseUser(
                id=data["id"],
                email=data.get("email", ""),
                name=data.get("user_metadata", {}).get("full_name"),
                avatar_url=data.get("user_metadata", {}).get("avatar_url"),
            )

            # Cache the result
            self._cache[token] = (user, time.monotonic() + _AUTH_CACHE_TTL)

            # Evict expired entries when cache grows
            if len(self._cache) > 100:
                now = time.monotonic()
                expired = [k for k, (_, exp) in self._cache.items() if exp <= now]
                for k in expired:
                    del self._cache[k]

            return user

        except Exception as e:
            logger.error("Supabase token validation error", error=str(e))
            return None

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    @staticmethod
    def supabase_uid_to_user_id(uid: str) -> int:
        """Convert a Supabase UUID to a deterministic integer user_id.

        Takes the lower 62 bits of the UUID, then sets bit 62 high.
        Result is always in [2^62, 2^63-1], which:
        - Fits in SQLite's signed 64-bit INTEGER
        - Avoids collision with Telegram user IDs (typically < 2^40)
        """
        uid_hex = uid.replace("-", "")
        raw = int.from_bytes(bytes.fromhex(uid_hex)[:8], "big")
        return (raw & ((1 << 62) - 1)) | (1 << 62)
