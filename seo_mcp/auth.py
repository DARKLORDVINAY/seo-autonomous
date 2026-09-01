"""Remote MCP verifies an operator-selected issuer and pinned public key.

The authorization server owns login and OAuth client registration. The MCP resource
server cannot mint tokens, trust arbitrary issuers, or reuse its internal API token
as a public-client credential.
"""
from __future__ import annotations

from pathlib import Path

import jwt
from mcp.server.auth.provider import AccessToken


class PinnedJWTVerifier:
    def __init__(self, issuer: str, audience: str, public_key_file: str, allowed_subjects: set[str]):
        if not issuer.startswith("https://") or not audience.startswith("https://") or not allowed_subjects:
            raise ValueError("Remote MCP requires HTTPS issuer/resource and an explicit subject allowlist")
        self.issuer, self.audience = issuer, audience
        self.key = Path(public_key_file).read_text()
        if "PRIVATE KEY" in self.key:
            raise ValueError("Configure a public verification key, never a signing key")
        self.allowed_subjects = allowed_subjects

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(token, self.key, algorithms=["RS256", "ES256"],
                                audience=self.audience, issuer=self.issuer,
                                options={"require": ["exp", "iat", "sub", "aud", "iss"]})
            if claims["sub"] not in self.allowed_subjects:
                return None
            scopes = claims.get("scope", "")
            if not isinstance(scopes, str) or "seo:read" not in scopes.split():
                return None
            return AccessToken(token=token, client_id=str(claims.get("client_id", claims.get("azp", ""))),
                               subject=claims["sub"], scopes=scopes.split(), expires_at=int(claims["exp"]),
                               resource=self.audience)
        except (jwt.PyJWTError, ValueError, KeyError, TypeError):
            return None
