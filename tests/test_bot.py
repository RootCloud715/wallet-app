"""Runs the real bot against the real app in-process (no server, no network)."""

import asyncio
import sys
from pathlib import Path

import httpx



from bot.bot import parse_args, run  # noqa: E402

from app.main import app  # noqa: E402
from tests.conftest import ADMIN  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_bots_fill_database_and_books_balance(client):
    # `client` fixture has already started the app (tables created).
    args = parse_args([
        "--base-url", "http://testserver", "--users", "8", "--duration", "4",
        "--think-ms", "30", "--seed", "7", "--admin-key", ADMIN["X-Admin-Key"],
    ])
    result = asyncio.run(run(args, transport=httpx.ASGITransport(app=app)))

    assert result["accounts"] == 8
    assert result["statuses"].get("201", 0) > 8  # accounts plus real transfers happened
    assert result["statuses"].get("422", 0) > 0  # overdraft attempts were refused
    assert result["statuses"].get("401", 0) > 0  # bad logins / missing tokens were refused
    assert not any(code.startswith("5") for code in result["statuses"])  # nothing crashed
    assert result["books"]["books_balance"] is True
    assert result["books"]["transactions"] > 8
