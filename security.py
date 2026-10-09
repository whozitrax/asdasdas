from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InvalidInitData(ValueError):
    """Telegram did not authenticate this Mini App request."""


def authenticated_user_id(
    init_data: str,
    bot_token: str,
    max_age: int = 3600,
    *,
    now: int | None = None,
) -> int:
    """Validate signed Telegram WebApp.initData with the current bot token.

    Bot-token HMAC is mandatory. Telegram clients and validators have used
    both data-check-string layouts when the separate third-party `signature`
    parameter is present. Verify either *cryptographically signed* layout.
    No unsigned/user-provided ID is ever trusted.
    """
    if not isinstance(init_data, str) or not init_data or len(init_data) > 16_384:
        raise InvalidInitData('Нет авторизации Telegram Mini App.')
    if not bot_token:
        raise InvalidInitData('Не настроен токен бота для Mini App.')
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InvalidInitData('Некорректный формат данных Telegram.') from exc
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise InvalidInitData('Повторные параметры авторизации.')

    params = dict(pairs)
    actual = params.pop('hash', '')
    if len(actual) != 64 or any(c not in '0123456789abcdefABCDEF' for c in actual):
        raise InvalidInitData('Отсутствует подпись Telegram.')
    actual = actual.lower()

    secret = hmac.new(b'WebAppData', bot_token.encode('utf-8'), hashlib.sha256).digest()

    def check(fields: dict[str, str]) -> bool:
        data_check_string = '\n'.join(f'{key}={fields[key]}' for key in sorted(fields))
        expected = hmac.new(secret, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        return hmac.compare_digest(actual, expected)

    # Telegram describes the token-HMAC payload as all fields except 'hash'.
    # Some clients omit the third-party Ed25519 'signature' from that payload.
    # This compatibility branch *still requires a valid token-based HMAC*.
    valid = check(params)
    if not valid and 'signature' in params:
        without_third_party_signature = dict(params)
        without_third_party_signature.pop('signature', None)
        valid = check(without_third_party_signature)
    if not valid:
        raise InvalidInitData('Неверная подпись Telegram.')

    current = int(time.time()) if now is None else now
    try:
        date = int(params['auth_date'])
        user = json.loads(params['user'])
        user_id = int(user['id'])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidInitData('Неполные данные пользователя.') from exc
    if date > current + 60 or current - date > max_age:
        raise InvalidInitData('Сессия устарела. Откройте Mini App заново.')
    if user_id <= 0:
        raise InvalidInitData('Некорректный пользователь.')
    return user_id
