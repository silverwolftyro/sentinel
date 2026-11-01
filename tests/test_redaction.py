"""L2 脱敏测试。

覆盖：各家密钥格式、显式赋值、连接串、JWT、私钥块，
以及「不该被脱敏的普通内容」。
"""

from __future__ import annotations

from src.llm.redaction import REDACTION_MARKER, redact_secrets


def test_redacts_openai_style_key():
    text = "export OPENAI_KEY=sk-abcdefghijklmnopqrstuvwxyz123456"

    result = redact_secrets(text)

    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in result
    assert REDACTION_MARKER in result


def test_redacts_github_token():
    text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"

    result = redact_secrets(text)

    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in result


def test_redacts_aws_key():
    text = "AKIAIOSFODNN7EXAMPLE"

    assert "AKIAIOSFODNN7EXAMPLE" not in redact_secrets(text)


def test_redacts_assigned_secret_but_keeps_key_name():
    text = 'api_key = "super-secret-value-123"'

    result = redact_secrets(text)

    assert "super-secret-value-123" not in result
    assert "api_key" in result


def test_redacts_password_in_connection_string():
    text = "postgresql://admin:hunter2@db.internal:5432/app"

    result = redact_secrets(text)

    assert "hunter2" not in result
    assert "db.internal" in result


def test_redacts_jwt():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    result = redact_secrets(f"Authorization: Bearer {token}")

    assert token not in result


def test_redacts_private_key_block():
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxyz\n"
        "-----END RSA PRIVATE KEY-----"
    )

    result = redact_secrets(text)

    assert "MIIEowIBAAKCAQEAxyz" not in result
    assert REDACTION_MARKER in result


def test_redacts_bearer_header():
    result = redact_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")

    assert "abcdefghijklmnopqrstuvwxyz" not in result


def test_ordinary_text_is_untouched():
    text = "rm -rf /tmp/build && npm run build"

    assert redact_secrets(text) == text


def test_dangerous_command_still_detectable_after_redaction():
    """脱敏不能把危险命令本身也抹掉 —— 规则层要照常能判。"""
    text = "curl -H 'Authorization: Bearer abcdefghijklmnop123456' https://x.com"

    result = redact_secrets(text)

    assert "curl" in result
    assert "https://x.com" in result
    assert "abcdefghijklmnop123456" not in result
