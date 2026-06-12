"""Google OAuth (OpenID Connect) client, via Authlib.

Registered only when GOOGLE_CLIENT_ID/SECRET are set (see config). Authlib uses
the request session (Starlette SessionMiddleware) to carry the OAuth state/nonce
across the redirect to Google and back, so this needs SESSION_SECRET set too —
which it always is in any environment where the Google button is shown.

Discovery URL gives us Google's authorize / token / jwks endpoints automatically,
and Authlib validates the returned id_token (signature + nonce) for us.
"""
from authlib.integrations.starlette_client import OAuth

from app.config import settings

oauth = OAuth()

if settings.google_oauth_enabled:
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
