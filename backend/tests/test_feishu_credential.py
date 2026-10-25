"""Feishu app credentials: FEISHU_APP_ID / FEISHU_APP_SECRET in agent-demo/.env.

The format check runs offline. The token check calls Feishu's auth API, so it is
an integration test and only runs on request:  uv run pytest -m integration
Both are skipped when the credentials are not set.
"""

import os

import httpx
import pytest
from dotenv import dotenv_values

from agent_demo.config import PROJECT_ROOT

# Read .env without changing os.environ, so other tests are not affected.
_env = {**dotenv_values(PROJECT_ROOT / ".env"), **os.environ}
APP_ID = (_env.get("FEISHU_APP_ID") or "").strip()
APP_SECRET = (_env.get("FEISHU_APP_SECRET") or "").strip()
DOMAIN = (_env.get("FEISHU_DOMAIN") or "https://open.feishu.cn").rstrip("/")

needs_credentials = pytest.mark.skipif(
    not (APP_ID and APP_SECRET), reason="FEISHU_APP_ID / FEISHU_APP_SECRET not set in .env"
)


@needs_credentials
def test_credentials_format():
    assert APP_ID.startswith("cli_"), "FEISHU_APP_ID should look like cli_xxxxxxxx (Credentials page)"
    assert not any(c.isspace() for c in APP_SECRET), "FEISHU_APP_SECRET contains whitespace"
    assert APP_SECRET != APP_ID, "FEISHU_APP_SECRET is the app ID, not the secret"


@pytest.mark.integration
@needs_credentials
def test_credentials_get_tenant_token():
    # The same call the Feishu SDK makes before any API request.
    resp = httpx.post(
        f"{DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    assert data.get("code") == 0, f"Feishu rejected the credentials: {data.get('code')} {data.get('msg')}"
    assert data["tenant_access_token"] and data["expire"] > 0
