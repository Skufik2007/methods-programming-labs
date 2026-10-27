"""Проверка JWT, выпущенных Go-сервисом (повышенное задание №3, часть Python).

Python не знает секрета подписи: он скачивает открытые ключи Go-сервиса из
/.well-known/jwks.json и проверяет по ним подпись RS256, а также iss, aud и сроки.
Ключи кешируются; при встрече неизвестного kid (Go сменил ключ) кеш обновляется,
но не чаще раза в MIN_REFRESH_INTERVAL, чтобы мусорные токены не устроили
DoS на Go-сервис.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger("lab10.auth")

ALGORITHM = "RS256"
LEEWAY_SECONDS = 30
MIN_REFRESH_INTERVAL = 30.0


class TokenError(Exception):
    """Токен недействителен — ответ 401."""


class JWKSUnavailableError(Exception):
    """Не удалось получить ключи Go-сервиса — ответ 503."""


class JWKSVerifier:
    def __init__(
        self,
        client: httpx.AsyncClient,
        jwks_url: str,
        issuer: str,
        audience: str,
        cache_ttl: float = 300.0,
        clock: Any = time.monotonic,
    ) -> None:
        self._client = client
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._audience = audience
        self._cache_ttl = cache_ttl
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched_at = float("-inf")
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise TokenError(f"некорректный формат токена: {exc}") from exc
        if header.get("alg") != ALGORITHM:
            raise TokenError(f"алгоритм {header.get('alg')!r} не разрешён, нужен {ALGORITHM}")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenError("в заголовке токена нет kid")

        key = await self._key_for(kid)
        try:
            return jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            raise TokenError(str(exc)) from exc

    async def _key_for(self, kid: str) -> Any:
        now = self._clock()
        fresh = now - self._fetched_at < self._cache_ttl
        if fresh and kid in self._keys:
            return self._keys[kid]

        async with self._lock:
            # Пока ждали блокировку, другой запрос мог уже обновить кеш.
            now = self._clock()
            if kid in self._keys and now - self._fetched_at < self._cache_ttl:
                return self._keys[kid]
            if kid not in self._keys and now - self._fetched_at < MIN_REFRESH_INTERVAL:
                raise TokenError(f"неизвестный kid {kid!r}")
            try:
                await self._refresh()
            except JWKSUnavailableError:
                if kid not in self._keys:
                    raise
                # Go недоступен, но ключ уже известен: истёкший TTL — не повод отклонять
                # валидные токены. Работаем на старом ключе, повтор — через MIN_REFRESH_INTERVAL.
                log.warning("JWKS недоступен, используется ранее полученный ключ %s", kid)
                self._fetched_at = now - self._cache_ttl + MIN_REFRESH_INTERVAL
                return self._keys[kid]

        if kid not in self._keys:
            raise TokenError(f"неизвестный kid {kid!r}")
        return self._keys[kid]

    async def _refresh(self) -> None:
        try:
            resp = await self._client.get(self._jwks_url)
            resp.raise_for_status()
            jwks = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JWKSUnavailableError(f"не удалось получить JWKS с {self._jwks_url}: {exc}") from exc

        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise JWKSUnavailableError(f"ответ {self._jwks_url} не в формате JWKS: ожидается {{'keys': [...]}}")

        keys: dict[str, Any] = {}
        for jwk in jwks["keys"]:
            if not isinstance(jwk, dict):
                continue
            if jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig" or "kid" not in jwk:
                continue
            try:
                keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm=ALGORITHM).key
            except jwt.PyJWKError:
                continue
        self._keys = keys
        self._fetched_at = self._clock()


_bearer = HTTPBearer(auto_error=False)


def _unauthorized(message: str, error: str | None = None) -> HTTPException:
    challenge = 'Bearer realm="lab10"' + (f', error="{error}"' if error else "")
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": error or "unauthorized", "message": message},
        headers={"WWW-Authenticate": challenge},
    )


async def require_claims(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any]:
    """FastAPI-зависимость: проверенные claims или 401/503."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("нужен заголовок Authorization: Bearer <token>")
    verifier: JWKSVerifier = request.app.state.verifier
    try:
        claims = await verifier.verify(credentials.credentials)
    except TokenError as exc:
        raise _unauthorized(f"токен недействителен: {exc}", "invalid_token") from exc
    except JWKSUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "auth_unavailable", "message": str(exc)},
        ) from exc
    request.state.user = claims["sub"]
    return claims
