# Wallet API

A small digital-wallet backend built with FastAPI. Users register, log in, check a balance, send mock money to each other, and view their transaction history. A bundled bot simulates many users, so the database fills with realistic traffic without manual work.

It is a practice project for DevOps and DevSecOps: the app is intentionally small so the focus stays on how it is built, tested, shipped, secured, and observed.

**Mock money only.** This project does not process real payments. Never store real card, bank, or personal data in it.

## Features

- **Accounts and auth:** registration, login, and JWT bearer tokens (30-minute lifetime)
- **Transfers:** send money to another user; balances can never go negative
- **Retry safety:** an `Idempotency-Key` header makes a retried transfer pay only once
- **History:** a private, paginated ledger per user
- **Bots:** concurrent simulated users, including deliberate mistakes (overdrafts, bad passwords, missing tokens)
- **Books check:** the bot can verify that total money held equals total money minted
- **Operations:** liveness and readiness endpoints, Prometheus metrics, and opt-in failure injection
- **Container image:** multi-stage build that runs as a non-root user
- **Tests:** a pytest suite that includes a race-condition test and a full bot run, with no server needed

## Quick start

Requires Python 3.10 or newer. Developed on 3.13 and also run on 3.12; the Docker image uses 3.13.1.

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

export JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export ADMIN_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(24))")

uvicorn app.main:app --port 8000
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

$env:JWT_SECRET = python -c "import secrets; print(secrets.token_urlsafe(48))"
$env:ADMIN_KEY  = python -c "import secrets; print(secrets.token_urlsafe(24))"

uvicorn app.main:app --port 8000
```

Then open http://localhost:8000/docs for interactive API docs. To call protected endpoints there, log in through `/auth/login`, click Authorize, and paste the token.

The app refuses to start unless `JWT_SECRET` is set to at least 32 characters. That is intentional.

## Run with Docker

The `Dockerfile` is a two-stage build. The first stage installs dependencies into a virtual environment, and the second stage copies only that environment and the `app/` folder onto a fresh `python:3.13.1-slim` base. It runs as a non-root user (`appuser`, UID 10001). No secrets are baked into the image: `JWT_SECRET` and `ADMIN_KEY` are passed at run time.

Build:

```bash
docker build -t wallet-app:dev .
```

Run (the secret must be at least 32 characters; `openssl rand -hex 32` gives 64):

```bash
docker run -d --name wallet -p 8000:8000 \
  -e JWT_SECRET="$(openssl rand -hex 32)" \
  -e ADMIN_KEY="$(openssl rand -hex 16)" \
  wallet-app:dev
```

Check it:

```bash
curl localhost:8000/health                # {"status":"ok"}
docker exec wallet whoami                 # appuser
docker exec wallet touch /app/app/x.txt   # Permission denied: the code is read-only
docker logs wallet                        # if it exits, read the last lines
docker rm -f wallet                       # stop and remove
```

Notes:

- The app listens on `0.0.0.0` inside the container so that the published port (`-p`) can reach it.
- Only `app/data` is writable by the app user. With the default SQLite database, the data file lives inside the container and is lost when the container is removed. Docker Compose with Postgres and a volume is the next roadmap item.
- `.dockerignore` keeps `.venv`, `data`, `.env`, and `.git` out of the image.
- Image size, measured with `docker images`: the single-stage non-root version was 318 MB on disk (74.7 MB compressed), and the multi-stage version is 322 MB (75.2 MB). This project has no compiler-heavy dependencies, so multi-stage does not shrink it. See `BROKEN.md` for the open question.

## Configuration

All settings are environment variables. Copy `.env.example` to `.env` for local use and never commit the real `.env`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `JWT_SECRET` | Yes | none | Signs login tokens. Random string, 32+ characters. Secret. |
| `ADMIN_KEY` | No | unset | Enables `/admin/*` endpoints. Without it they return 404. Secret. |
| `DATABASE_URL` | No | `sqlite:///./data/wallet.db` | Database connection. Use a `postgresql://` URL for Postgres. |
| `ENABLE_CHAOS` | No | `false` | Allows the failure-injection switches. Never enable in production. |
| `SIGNUP_BONUS_CENTS` | No | `100000` | Mock balance given at signup (100000 = 1,000.00). |

## API

Money is always integer cents: `amount_cents: 2500` means 25.00.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/register` | None | Create an account and receive the signup bonus |
| POST | `/auth/login` | None | Exchange username and password for a bearer token |
| GET | `/me` | Token | Current username and balance |
| POST | `/transfers` | Token | Send money. Supports an `Idempotency-Key` header |
| GET | `/transactions` | Token | Your history, newest first (`limit` 1-100, `offset`) |
| GET | `/health` | None | Liveness: the process is up |
| GET | `/ready` | None | Readiness: the database is reachable (503 if not) |
| GET | `/metrics` | None | Prometheus metrics |
| GET | `/admin/stats` | Admin key | Totals and the books-balance check |
| GET, POST | `/admin/chaos` | Admin key | Inject latency or errors (needs `ENABLE_CHAOS=true`) |

Admin endpoints take the key in an `X-Admin-Key` header.

Rules worth knowing: usernames are 3-32 characters (letters, digits, underscore) and case-insensitive; passwords are 8-128 characters; one transfer is at most 10,000.00. Errors are 404 for an unknown recipient, 400 for a transfer to yourself, 422 for insufficient funds or invalid input, and 401 for a missing or invalid token. Retrying a transfer with the same `Idempotency-Key` returns the original result with 200 instead of paying again.

### Example

```bash
# register and log in
curl -X POST localhost:8000/auth/register -H "content-type: application/json" \
     -d '{"username":"alice","password":"correct-horse-battery"}'
TOKEN=$(curl -s -X POST localhost:8000/auth/login -H "content-type: application/json" \
     -d '{"username":"alice","password":"correct-horse-battery"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# check balance
curl localhost:8000/me -H "Authorization: Bearer $TOKEN"
```

## Bot users

The bot registers accounts, logs in, and then loops through transfers, balance checks, and history reads. About one action in six is a deliberate mistake (overdraft, wrong password, no token, unknown recipient), and some transfers are sent twice with the same idempotency key to confirm they pay once.

```bash
python bot/bot.py --users 20 --duration 120      # 20 bots for two minutes
python bot/bot.py --users 50 --duration 0        # run until Ctrl+C
python bot/bot.py --base-url http://localhost:8000 --think-ms 100
```

| Flag | Default | Meaning |
|---|---|---|
| `--base-url` | `http://localhost:8000` | Where the API is running |
| `--users` | 10 | Number of bot accounts |
| `--duration` | 60 | Seconds to run; 0 means until Ctrl+C |
| `--think-ms` | 200 | Average pause between actions |
| `--timeout` | 10 | Per-request timeout in seconds |
| `--seed` | none | Fix the random seed for repeatable runs |
| `--admin-key` | `$ADMIN_KEY` | If set, the bot checks the books at the end |

With `ADMIN_KEY` set in the bot's environment, it ends with a line like:

```
Books: minted 800000 cents, held 800000 cents -> BALANCED
```

`BALANCED` means every transfer was atomic. `MISMATCH` means the app has a bug.

## Tests

```bash
pytest -q
```

The suite covers registration and login, password hashing, forged, expired, and unsigned tokens, transfer rules, idempotent retries, concurrent transfers that must not overdraw a wallet, history privacy and pagination, admin access, failure injection, and a complete bot run whose books must balance. It uses a throwaway database and its own secrets, so no environment setup is needed.

## Observability

`GET /metrics` exposes:

- `http_requests_total{method,path,status}` and `http_request_duration_seconds{method,path}`
- `wallet_registrations_total`
- `wallet_logins_total{result}`
- `wallet_transfers_total{result}` (ok, insufficient_funds, unknown_recipient, self_transfer)
- `wallet_money_moved_cents_total`

### Failure injection

With `ENABLE_CHAOS=true`, an admin can slow down or break the app on purpose to practice finding failures. Health, readiness, metrics, and admin endpoints are never affected, so probes stay honest.

```bash
curl -X POST localhost:8000/admin/chaos -H "X-Admin-Key: $ADMIN_KEY" \
     -H "content-type: application/json" -d '{"latency_ms": 500, "error_rate": 0.2}'
```

## Design notes

- **Integer cents** for all money, since floats cannot represent values like 0.10 exactly.
- **Atomic transfers.** The debit is a single conditional `UPDATE ... WHERE balance >= amount`, so two simultaneous requests cannot both spend the same money. A database `CHECK` constraint refuses a negative balance as a second line of defense.
- **Append-only ledger.** Every balance change has a transaction row, which is what lets `/admin/stats` prove the books balance.
- **Passwords** are hashed with scrypt and a per-user salt. Login takes similar time and gives the same message for an unknown user and a wrong password.
- **Tokens** are signed with a pinned algorithm, require an expiry, and reject unsigned or forged tokens.
- **Fail fast.** The app will not start with a missing or short `JWT_SECRET`.
- **Admin surface is opt-in.** Without `ADMIN_KEY`, admin routes return 404; with it, the key is compared in constant time.
- **Postgres row locking.** On Postgres, both wallets are locked in a fixed order so opposite transfers cannot deadlock. SQLite needs no row locks because it serializes writers.
- **Least privilege in the container.** The image runs as a non-root user, the application code is read-only to that user, and only the data folder is writable.

## Known limitations

These are deliberate gaps, and several are targets for later hardening work:

- Only tested against SQLite so far. The Postgres code path is written but not yet verified against a real Postgres.
- SQLite allows one writer at a time, which limits write throughput under heavy load.
- In the container, the default SQLite file is lost when the container is removed.
- No rate limiting or lockout on login attempts.
- No token revocation or refresh tokens.
- No HTTPS in the app itself; TLS is expected to be terminated in front of it.
- `/metrics` is unauthenticated and should be restricted in any real deployment.
- Tables are created at startup. A real deployment would use migrations (for example Alembic).

## What broke

Problems I hit while building and testing this project are logged in [BROKEN.md](BROKEN.md): the symptom, how I found the cause, the fix, and the lesson. It includes wrong guesses and questions that are still open.

## Project structure

```
wallet-app/
├── app/
│   ├── __init__.py
│   ├── database.py      # engine, sessions, SQLite/Postgres setup
│   ├── models.py        # User and Transaction tables
│   ├── security.py      # password hashing and JWT helpers
│   └── main.py          # API routes, metrics, failure injection
├── bot/
│   ├── __init__.py
│   └── bot.py           # simulated users
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_wallet.py
│   └── test_bot.py
├── .dockerignore        # keeps .venv, data, .env, .git out of the image
├── .env.example
├── .gitignore
├── BROKEN.md            # log of real problems I hit and how I fixed them
├── Dockerfile           # multi-stage build, non-root user
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

## Roadmap

- [x] Container image (multi-stage, non-root)
- [ ] Docker Compose with Postgres
- [ ] CI pipeline: lint, test, build, and publish an image on every push
- [ ] Infrastructure as code (Terraform on AWS)
- [ ] Kubernetes deployment with GitOps
- [ ] Monitoring: Prometheus, Grafana, Loki, alerts, and an SLO
- [ ] Security scanning in the pipeline: secrets, code, dependencies, images, IaC, signing
- [ ] Incident drill with a runbook and postmortem
