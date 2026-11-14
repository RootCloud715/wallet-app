"""Bot users for the wallet API. They fill the database without you touching anything.

Each bot registers an account, logs in, then loops: check balance, send money to a
random other bot, read history. Some actions are wrong on purpose (overdraft, bad
password, no token, unknown recipient) so your dashboards show realistic errors.

At the end, if you pass --admin-key, it asks the API to check the books: total money
held must equal total money minted. If it does not, the app has a bug.

Examples:
    python bot/bot.py                                   # 10 bots for 60 seconds
    python bot/bot.py --users 50 --duration 0           # 50 bots until Ctrl+C
    python bot/bot.py --base-url http://wallet:8000 --admin-key $ADMIN_KEY
"""

import argparse
import asyncio
import os
import random
import secrets
import time
from collections import Counter

import httpx

# (action, weight). Transfers and reads dominate; mistakes are rare.
ACTIONS = [
    ("transfer", 40),
    ("balance", 20),
    ("history", 15),
    ("overdraft", 5),
    ("bad_login", 5),
    ("no_token", 4),
    ("unknown_recipient", 3),
    ("retry_transfer", 8),  # same Idempotency-Key twice: must pay only once
]


class Recorder:
    def __init__(self) -> None:
        self.statuses: Counter = Counter()
        self.actions: Counter = Counter()
        self.latencies: list[float] = []
        self.start = time.perf_counter()

    def add(self, action: str, status: str, seconds: float) -> None:
        self.actions[action] += 1
        self.statuses[status] += 1
        self.latencies.append(seconds)

    def report(self) -> str:
        elapsed = max(time.perf_counter() - self.start, 0.001)
        total = len(self.latencies)
        lines = [f"\nRan {elapsed:.1f}s, {total} requests ({total / elapsed:.1f}/s)"]
        if total:
            lat = sorted(self.latencies)
            p50 = lat[int(total * 0.50)] * 1000
            p95 = lat[min(total - 1, int(total * 0.95))] * 1000
            lines.append(f"Latency: p50 {p50:.0f} ms, p95 {p95:.0f} ms")
        lines.append("Actions: " + ", ".join(f"{k}={v}" for k, v in sorted(self.actions.items())))
        lines.append("Status:  " + ", ".join(f"{k}={v}" for k, v in sorted(self.statuses.items())))
        return "\n".join(lines)


async def bot_user(
    client: httpx.AsyncClient,
    usernames: list[str],
    rec: Recorder,
    stop_at: float | None,
    think_ms: int,
    run_id: str,
    index: int,
) -> None:
    username = f"bot_{run_id}_{index}"
    password = secrets.token_urlsafe(16)
    token: str | None = None
    names, weights = zip(*ACTIONS)

    async def call(action, method, path, auth=True, **kwargs):
        headers = kwargs.pop("headers", {})
        if auth and token:
            headers["Authorization"] = f"Bearer {token}"
        t0 = time.perf_counter()
        try:
            resp = await client.request(method, path, headers=headers, **kwargs)
            rec.add(action, str(resp.status_code), time.perf_counter() - t0)
            return resp
        except httpx.HTTPError as exc:  # connection refused, timeout... shows outages in the report
            rec.add(action, type(exc).__name__, time.perf_counter() - t0)
            return None

    async def login() -> None:
        nonlocal token
        resp = await call("login", "POST", "/auth/login", auth=False,
                          json={"username": username, "password": password})
        token = resp.json()["access_token"] if resp is not None and resp.status_code == 200 else None

    # Stagger starts so all bots do not register at the same instant.
    await asyncio.sleep(random.uniform(0, think_ms / 1000))
    resp = await call("register", "POST", "/auth/register", auth=False,
                      json={"username": username, "password": password})
    if resp is None or resp.status_code != 201:
        return  # could not create the account; nothing more this bot can do
    usernames.append(username)
    await login()

    while stop_at is None or time.perf_counter() < stop_at:
        if token is None:
            await login()
            await asyncio.sleep(1)
            continue

        action = random.choices(names, weights)[0]
        others = [u for u in usernames if u != username]
        if action in ("transfer", "retry_transfer") and not others:
            action = "balance"

        if action == "transfer":
            await call("transfer", "POST", "/transfers",
                       json={"to": random.choice(others), "amount_cents": random.randint(100, 20000)},
                       headers={"Idempotency-Key": secrets.token_hex(8)})
        elif action == "retry_transfer":
            body = {"to": random.choice(others), "amount_cents": random.randint(100, 5000)}
            key = {"Idempotency-Key": secrets.token_hex(8)}
            await call("retry_transfer", "POST", "/transfers", json=body, headers=dict(key))
            await call("retry_transfer", "POST", "/transfers", json=body, headers=dict(key))
        elif action == "balance":
            resp = await call("balance", "GET", "/me")
            if resp is not None and resp.status_code == 401:
                token = None  # token expired, log in again
        elif action == "history":
            await call("history", "GET", "/transactions", params={"limit": random.choice([5, 10, 20])})
        elif action == "overdraft":
            if others:
                await call("overdraft", "POST", "/transfers",
                           json={"to": random.choice(others), "amount_cents": 1_000_000})
        elif action == "bad_login":
            await call("bad_login", "POST", "/auth/login", auth=False,
                       json={"username": username, "password": "definitely-wrong-pw"})
        elif action == "no_token":
            await call("no_token", "GET", "/me", auth=False)
        elif action == "unknown_recipient":
            await call("unknown_recipient", "POST", "/transfers",
                       json={"to": "nobody_" + secrets.token_hex(4), "amount_cents": 100})

        # Pause like a person would; the average is --think-ms.
        await asyncio.sleep(random.expovariate(1000 / max(think_ms, 1)))


async def run(args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Run the bots. `transport` lets tests call the app in-process without a server."""
    if args.seed is not None:
        random.seed(args.seed)

    rec = Recorder()
    usernames: list[str] = []
    run_id = secrets.token_hex(3)
    stop_at = None if args.duration == 0 else time.perf_counter() + args.duration

    async with httpx.AsyncClient(
        base_url=args.base_url,
        timeout=args.timeout,
        limits=httpx.Limits(max_connections=max(args.users, 10)),
        transport=transport,
    ) as client:
        print(
            f"Bots started: {args.users} users -> {args.base_url} "
            f"({'until Ctrl+C' if stop_at is None else f'{args.duration}s'})"
        )
        tasks = [
            asyncio.create_task(
                bot_user(client, usernames, rec, stop_at, args.think_ms, run_id, i)
            )
            for i in range(args.users)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            print(rec.report())
            print(f"Accounts created: {len(usernames)}")

        books = None
        if args.admin_key:
            resp = await client.get("/admin/stats", headers={"X-Admin-Key": args.admin_key})
            if resp.status_code == 200:
                books = resp.json()
                print(f"Books: minted {books['money_minted_cents']} cents, "
                      f"held {books['money_held_cents']} cents -> "
                      f"{'BALANCED' if books['books_balance'] else 'MISMATCH, THERE IS A BUG'}")
            else:
                print(f"Could not read /admin/stats (HTTP {resp.status_code}); check --admin-key")

    return {"statuses": dict(rec.statuses), "accounts": len(usernames), "books": books}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bot users for the wallet API")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--users", type=int, default=10, help="number of bot accounts")
    p.add_argument("--duration", type=int, default=60, help="seconds to run, 0 = until Ctrl+C")
    p.add_argument("--think-ms", type=int, default=200, help="average pause between actions")
    p.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    p.add_argument("--seed", type=int, default=None, help="fix the random seed for repeat runs")
    p.add_argument("--admin-key", default=os.getenv("ADMIN_KEY"),
                   help="if given, check that the books balance at the end (default: $ADMIN_KEY)")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass
