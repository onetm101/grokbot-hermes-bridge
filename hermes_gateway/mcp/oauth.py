"""Small, single-user OAuth provider for the Hermes MCP gateway.

The gateway keeps its existing high-entropy ``HERMES_BRIDGE_SECRET`` as the
owner credential while exposing the standard MCP OAuth discovery and PKCE
flow expected by remote clients such as Grok Bot.  OAuth access and refresh
tokens are signed, self-contained values; dynamic client registrations are
persisted in a mode-0600 JSON file so restarts do not break the client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import GatewayConfig

_SCOPE = "hermes"
_ACCESS_TTL = 3600
_REFRESH_TTL = 30 * 24 * 3600
_CODE_TTL = 300
_PENDING_TTL = 600

logger = logging.getLogger("hermes_gateway.mcp.oauth")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class HermesOAuthProvider:
    """Single-owner MCP OAuth provider backed by a runtime-only secret."""

    def __init__(self, config: GatewayConfig):
        if not config.secret or not config.public_base_url:
            raise ValueError("OAuth requires a secret and public base URL")
        self._secret = config.secret
        self._key = hashlib.sha256(("hermes-oauth-v1\0" + config.secret).encode()).digest()
        self._base_url = config.public_base_url.rstrip("/")
        self._clients_path = Path(config.oauth_clients_file).expanduser()
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, tuple[float, OAuthClientInformationFull, AuthorizationParams]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._load_clients()

    def _load_clients(self) -> None:
        try:
            raw = json.loads(self._clients_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for client_id, value in raw.items():
                    client = OAuthClientInformationFull.model_validate(value)
                    if client.client_id == client_id:
                        self._clients[client_id] = client
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return

    def _save_clients(self) -> None:
        self._clients_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._clients_path.with_suffix(self._clients_path.suffix + ".tmp")
        payload = {key: value.model_dump(mode="json") for key, value in self._clients.items()}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._clients_path)
            os.chmod(self._clients_path, 0o600)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id or not client_info.redirect_uris:
            raise RegistrationError("invalid_client_metadata", "client_id and redirect_uris are required")
        self._clients[client_info.client_id] = client_info
        self._save_clients()

    def _prune(self) -> None:
        now = time.time()
        self._pending = {key: value for key, value in self._pending.items() if value[0] >= now}
        self._codes = {key: value for key, value in self._codes.items() if value.expires_at >= now}

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._prune()
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = (time.time() + _PENDING_TTL, client, params)
        return f"{self._base_url}/oauth/approve?request={request_id}"

    async def approval(self, request: Request) -> Response:
        self._prune()
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = self._pending.get(request_id)
            if pending is None:
                return HTMLResponse(self._page("Demande expirée", "Relance l’authentification depuis Grok Bot."), 400)
            client_name = pending[1].client_name or "Grok Bot"
            return HTMLResponse(self._approval_page(request_id, client_name), headers={"Cache-Control": "no-store"})

        body = (await request.body()).decode("utf-8", "replace")
        form = parse_qs(body, keep_blank_values=True)
        request_id = (form.get("request") or [""])[0]
        owner_secret = (form.get("secret") or [""])[0]
        action = (form.get("action") or ["deny"])[0]
        pending = self._pending.get(request_id)
        if pending is None:
            return HTMLResponse(self._page("Demande expirée", "Relance l’authentification depuis Grok Bot."), 400)
        _, client, params = pending
        if action != "approve":
            self._pending.pop(request_id, None)
            return RedirectResponse(
                construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state),
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        provided = hashlib.sha256(owner_secret.encode()).digest()
        expected = hashlib.sha256(self._secret.encode()).digest()
        if not hmac.compare_digest(provided, expected):
            return HTMLResponse(self._approval_page(request_id, client.client_name or "Grok Bot", invalid=True), 403)

        self._pending.pop(request_id, None)
        code_value = secrets.token_urlsafe(32)
        code = AuthorizationCode(
            code=code_value,
            scopes=params.scopes or [_SCOPE],
            expires_at=time.time() + _CODE_TTL,
            client_id=client.client_id or "",
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        self._codes[code_value] = code
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code_value, state=params.state),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        self._prune()
        code = self._codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        logger.info("hermes oauth authorization code exchanged (client=%s)", client.client_id)
        return self._issue_tokens(
            client.client_id or "",
            authorization_code.scopes,
            authorization_code.resource,
            authorization_code.subject,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        payload = self._decode_token(refresh_token, "refresh")
        if payload is None or payload.get("client_id") != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=payload["client_id"],
            scopes=payload.get("scopes") or [_SCOPE],
            expires_at=payload["exp"],
            subject=payload.get("subject"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        logger.info("hermes oauth refresh token exchanged (client=%s)", client.client_id)
        return self._issue_tokens(client.client_id or "", scopes, None, refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        # The owner secret is only used on the approval page. It is never a
        # valid bearer credential for the MCP endpoint itself.
        payload = self._decode_token(token, "access")
        if payload is None:
            return None
        return AccessToken(
            token=token,
            client_id=payload["client_id"],
            scopes=payload.get("scopes") or [_SCOPE],
            expires_at=payload["exp"],
            resource=payload.get("resource"),
            subject=payload.get("subject"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        return None

    def _issue_tokens(
        self, client_id: str, scopes: list[str], resource: Optional[str], subject: Optional[str]
    ) -> OAuthToken:
        access = self._encode_token("access", client_id, scopes, _ACCESS_TTL, resource, subject)
        refresh = self._encode_token("refresh", client_id, scopes, _REFRESH_TTL, resource, subject)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=_ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _encode_token(
        self,
        kind: str,
        client_id: str,
        scopes: list[str],
        ttl: int,
        resource: Optional[str],
        subject: Optional[str],
    ) -> str:
        now = int(time.time())
        payload = {
            "kind": kind,
            "client_id": client_id,
            "scopes": scopes,
            "iat": now,
            "exp": now + ttl,
            "jti": secrets.token_urlsafe(16),
            "resource": resource,
            "subject": subject,
        }
        encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64(hmac.new(self._key, encoded.encode(), hashlib.sha256).digest())
        return f"hb1.{encoded}.{signature}"

    def _decode_token(self, token: str, expected_kind: str) -> dict | None:
        try:
            prefix, encoded, signature = token.split(".", 2)
            if prefix != "hb1":
                return None
            expected = _b64(hmac.new(self._key, encoded.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(_unb64(encoded))
            if payload.get("kind") != expected_kind or int(payload.get("exp", 0)) < int(time.time()):
                return None
            return payload
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _page(title: str, message: str) -> str:
        return (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Hermes Bridge</title><style>body{font:16px system-ui;max-width:520px;margin:12vh auto;"
            "padding:24px;color:#171717}main{border:1px solid #ddd;border-radius:16px;padding:28px}"
            "</style><main><h1>" + html.escape(title) + "</h1><p>" + html.escape(message) + "</p></main>"
        )

    @classmethod
    def _approval_page(cls, request_id: str, client_name: str, invalid: bool = False) -> str:
        warning = "<p style='color:#b42318'>Incorrect code. Try again.</p>" if invalid else ""
        return (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Authorize Hermes Bridge</title><style>body{font:16px system-ui;max-width:520px;margin:8vh auto;"
            "padding:24px;color:#171717}main{border:1px solid #ddd;border-radius:16px;padding:28px}"
            "input,button{box-sizing:border-box;width:100%;padding:12px;margin-top:12px;border-radius:10px;"
            "border:1px solid #bbb}button{background:#111;color:white;border:0;font-weight:650}"
            ".deny{background:#fff;color:#333;border:1px solid #bbb}</style><main>"
            "<h1>Authorize Hermes Bridge</h1><p>" + html.escape(client_name) +
            " is requesting access to the two Hermes tools.</p>" + warning +
            "<form method=post action='/oauth/approve'>"
            "<input type=hidden name=request value='" + html.escape(request_id, quote=True) + "'>"
            "<label>Private owner code<input type=password name=secret required autofocus autocomplete=current-password></label>"
            "<button name=action value=approve>Authorize</button>"
            "<button class=deny name=action value=deny>Deny</button></form></main>"
        )
