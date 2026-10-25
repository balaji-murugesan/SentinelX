"""Masking of secrets and account IDs (observability spec; task 3.2).

One test case per configured key name, so removing a key from the list fails a test.
"""

import pytest
from pydantic import SecretStr

from contracts.masking import MASK, mask_account, mask_value, scrub_text

SECRET_VALUE = "s3cr3t-VALUE-123"

SECRET_KEYS = [
    "token",
    "access_token",
    "accessToken",
    "refresh_token",
    "secret",
    "client_secret",
    "password",
    "db_password",
    "passwd",
    "authorization",
    "Authorization",
    "api_key",
    "api-key",
    "apikey",
    "access_key",
    "credential",
    "credentials",
    "cookie",
    "set-cookie",
    "private_key",
    "dsn",
    "connection_string",
    "TELEGRAM_BOT_TOKEN",
]


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_secret_named_key_is_masked(key: str) -> None:
    assert mask_value(SECRET_VALUE, key) == MASK


@pytest.mark.parametrize("key", SECRET_KEYS[:5])
def test_secret_key_masks_whole_structure(key: str) -> None:
    assert mask_value({"a": SECRET_VALUE}, key) == MASK
    assert mask_value([SECRET_VALUE], key) == MASK


@pytest.mark.parametrize("key", ["token", "password"])
@pytest.mark.parametrize("empty", [None, ""])
def test_empty_secret_is_left_alone(key: str, empty: object) -> None:
    assert mask_value(empty, key) == empty


@pytest.mark.parametrize(
    "key", ["account_id", "client_id", "client_code", "dhan_client_id", "ACCOUNT_ID"]
)
def test_account_keys_keep_only_last_four(key: str) -> None:
    assert mask_value("1100123456", key) == "****3456"


def test_account_id_as_int_and_short_values() -> None:
    assert mask_value(1100123456, "account_id") == "****3456"
    assert mask_value("1234", "account_id") == "****"
    assert mask_value("12", "client_id") == "****"
    assert mask_account("abcdefgh") == "****efgh"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("order_id", "ORD-9988776655"),
        ("idempotency_key", "idem-abc-123"),
        ("trace_id", "0af7651916cd43dd8448eb211c80319c"),
        ("symbol", "NIFTY24OCT25000CE"),
        ("price", "101.25"),
        ("qty", 75),
        ("account_idx", "1100123456"),  # not an account key
    ],
)
def test_ordinary_fields_are_untouched(key: str, value: object) -> None:
    assert mask_value(value, key) == value


def test_secretstr_is_masked_under_any_key() -> None:
    assert mask_value(SecretStr(SECRET_VALUE), "harmless_name") == MASK


def test_nested_structures_are_masked() -> None:
    event = {
        "headers": {"Authorization": f"Bearer {SECRET_VALUE}", "Accept": "json"},
        "orders": [{"order_id": "1", "client_id": "1100123456"}],
        "params": ("a", {"password": SECRET_VALUE}),
    }
    masked = mask_value(event)
    assert masked["headers"] == {"Authorization": MASK, "Accept": "json"}
    assert masked["orders"] == [{"order_id": "1", "client_id": "****3456"}]
    assert masked["params"] == ("a", {"password": MASK})


def test_very_deep_nesting_is_not_logged_raw() -> None:
    deep: dict[str, object] = {"x": SECRET_VALUE}
    for _ in range(20):
        deep = {"n": deep}
    assert SECRET_VALUE not in str(mask_value(deep))


# ---- content-based scrubbing (works even when the key name is harmless) -------------


def test_password_in_url_is_scrubbed() -> None:
    text = "connect to redis://user:hunter2@redis:6379/0 failed"
    assert scrub_text(text) == f"connect to redis://user:{MASK}@redis:6379/0 failed"
    assert "hunter2" not in scrub_text("postgresql+asyncpg://u:hunter2@db/x")
    assert "hunter2" not in scrub_text("redis://:hunter2@redis")


def test_url_without_password_is_unchanged() -> None:
    text = "GET https://api.dhan.co/v2/orders and http://user@host/x"
    assert scrub_text(text) == text


def test_bearer_header_is_scrubbed() -> None:
    assert scrub_text("sent Authorization: Bearer abc.DEF-123_x") == f"sent Authorization: {MASK}"
    assert scrub_text("bearer abc123") == f"Bearer {MASK}"


def test_jwt_access_token_is_scrubbed() -> None:
    jwt = "eyJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIn0.c2lnbmF0dXJl"  # fake, shape only
    assert jwt not in scrub_text(f"token was {jwt} ok")
    assert "ok" in scrub_text(f"token was {jwt} ok")


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter2",
        "password: hunter2",
        'password="hunter 2"',
        "token='hunter2'",
        "access_token=hunter2&x=1",
        "api_key = hunter2, next",
        "SECRET=hunter2;",
    ],
)
def test_key_value_pairs_in_text_are_scrubbed(text: str) -> None:
    assert "hunter" not in scrub_text(text)
    assert MASK in scrub_text(text)


def test_scrubbing_applies_to_values_under_harmless_keys() -> None:
    assert "hunter2" not in mask_value("login with password=hunter2", "message")


def test_plain_text_is_unchanged() -> None:
    text = "order 42 filled at 101.25, tokens of affection (no secret here)"
    # 'tokens of' has no '=' or ':' pair, so nothing should match.
    assert scrub_text(text) == text
