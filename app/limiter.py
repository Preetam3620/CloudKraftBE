from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import IS_VERCEL


def _client_ip(request: Request) -> str:
    # Behind Vercel's proxy request.client is the edge, not the user — every
    # visitor would share one bucket. Vercel puts the real address in x-real-ip.
    if IS_VERCEL:
        forwarded = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "")
        ip = forwarded.split(",")[0].strip()
        if ip:
            return ip
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip, default_limits=["200/minute"])
