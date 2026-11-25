# Wallet API

[![CI](https://github.com/RootCloud715/wallet-app/actions/workflows/ci.yml/badge.svg)](https://github.com/RootCloud715/wallet-app/actions/workflows/ci.yml)

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
- **Docker Compose stack:** the app and Postgres 16 with a health check and a persistent volume, started with one command
- **CI pipeline:** GitHub Actions tests every pull request and push, and publishes a non-root image to GHCR tagged with the commit SHA
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

Then open <http://localhost:8000/docs> for interactive API docs. To call protected endpoints there, log in through `/auth/login`, click **Authorize**, and paste the token.

The app refuses to start unless `JWT_SECRET` is set to at least 32 characters. That is intentional.

## Run with Docker

The Dockerfile is a two-stage build. The first stage installs dependencies into a virtual environment, and the second stage copies only that environment and the `app/` folder onto a fresh `python:3.13.1-slim` base. It runs as a non-root user (`appuser`, UID 10001). No secrets are baked into the image: `JWT_SECRET` and `ADMIN_KEY` are passed at run time.

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
- Only `app/data` is writable by the app user. Run on its own like this, the app uses its default SQLite database, and the data file lives inside the container and is lost when the container is removed. Use the Compose stack below for data that survives.
- `.dockerignore` keeps `.venv`, `data`, `.env`, and `.git` out of the image.
- Image size, measured with `docker images`: the single-stage non-root version was 318 MB on disk (74.7 MB compressed), and the multi-stage version is 322 MB (75.2 MB). The image built by CI measures the same, 322 MB on disk and 75.2 MB compressed. This project has no compiler-heavy dependencies, so multi-stage does not shrink it. See `BROKEN.md` for the open question.

## Run with Docker Compose (app + Postgres)

The `docker-compose.yml` file starts two services:

- **db:** Postgres 16 with a health check (`pg_isready`) and a named volume, `pgdata`, so data survives restarts. Its port is not published, so only the app can reach it.
- **app:** built from the Dockerfile and published on port 8000. It starts only after the database reports healthy, and Compose sets its `DATABASE_URL` to point at the `db` service.

Setup:

```bash
cp .env.example .env
# edit .env: set JWT_SECRET, ADMIN_KEY and the three POSTGRES_* values
# (letters and digits only in the password, so the connection URL needs no encoding)
docker compose up --build
```

Never commit the real `.env`. It is listed in `.gitignore` and `.dockerignore`.

Check it, from a second terminal:

```bash
docker compose ps                    # both services up, db shows (healthy)
curl localhost:8000/health           # {"status":"ok"}
docker compose exec app whoami       # appuser
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c '\dt'   # users and transactions tables
```

Run the bot against it. Export the admin key first (this prints nothing), otherwise the bot silently skips the books check:

```bash
export ADMIN_KEY=$(grep '^ADMIN_KEY=' .env | cut -d= -f2-)
python bot/bot.py --users 20 --duration 60 --base-url http://localhost:8000
```

A healthy run ends with `Books: minted ... cents, held ... cents -> BALANCED`.

Stopping:

```bash
docker compose down        # stops the stack, keeps the data volume
docker compose down -v     # also deletes the volume, so all data is lost
```

What has been verified on this stack (Postgres 16, 20 bot users):

- Both runs of 120 seconds and one of 60 seconds finished with no server errors, and the books balanced at the end.
- The row-locking code ran under concurrent transfers without deadlocks.
- The user count was identical before and after `docker compose down` and `up -d`.
- Throughput was about 52 requests per second with p95 around 350 ms, slower than the earlier SQLite figures (about 80 per second, p95 156 ms). This is not a controlled comparison and the cause is not measured yet; see `BROKEN.md`.

## Continuous integration (GitHub Actions)

The workflow lives in `.github/workflows/ci.yml` and runs on every pull request and on every push to `main`. It has two jobs.

| Job | Runs on | What it does |
|---|---|---|
| `test` | Pull requests and pushes to `main` | Checks out the code, sets up Python 3.13, installs `requirements.txt` and `requirements-dev.txt`, runs `pytest`, then builds the Docker image without pushing it |
| `publish` | Pushes to `main` only, after `test` passes | Logs in to GitHub Container Registry and pushes the image tagged with the commit SHA |

How it is set up:

- **Least privilege.** The workflow's default token permission is read-only (`contents: read`). Only the `publish` job also gets `packages: write`.
- **No stored secrets.** The `publish` job logs in with the built-in `GITHUB_TOKEN`, which GitHub creates for each run. The `JWT_SECRET` and `ADMIN_KEY` values in the `test` step are throwaway strings that only let the tests start. They protect nothing.
- **Pull requests never publish.** The `publish` job is skipped unless the event is a push to `main`.
- **Images are tagged by commit SHA.** Every image maps to exactly one commit, so a tag always means the same code.
- **Python version.** CI installs `3.13` and takes the newest patch release available on the runner. The exact `3.13.1` is not available there, and the Docker image still pins `3.13.1` through its base image. See `BROKEN.md`.
- **Actions are pinned by major version tag** (for example `@v7`), not by commit SHA. Pinning by SHA is a planned hardening step.

Pull a published image (replace the tag with a commit SHA shown under the repository's **Packages**; the package is public, so no login is needed):

```bash
docker pull ghcr.io/rootcloud715/wallet-app:<commit-sha>
docker run --rm ghcr.io/rootcloud715/wallet-app:<commit-sha> whoami   # appuser
```

Checked on the first image the pipeline published: it pulled without logging in, and `whoami` printed `appuser`.

## Configuration

All settings are environment variables. Copy `.env.example` to `.env` for local use and never commit the real `.env`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `JWT_SECRET` | Yes | none | Signs login tokens. Random string, 32+ characters. **Secret.** |
| `ADMIN_KEY` | No | unset | Enables `/admin/*` endpoints. Without it they return 404. **Secret.** |
| `DATABASE_URL` | No | `sqlite:///./data/wallet.db` | Database connection. Use a `postgresql://` URL for Postgres. Docker Compose sets this itself, so leave it out of `.env` when using Compose. |
| `ENABLE_CHAOS` | No | `false` | Allows the failure-injection switches. Never enable in production. |
| `SIGNUP_BONUS_CENTS` | No | `100000` | Mock balance given at signup (100000 = 1,000.00). |
| `POSTGRES_USER` | Compose only | none | Database user created by the Postgres container. |
| `POSTGRES_PASSWORD` | Compose only | none | Password for that user. **Secret.** |
| `POSTGRES_DB` | Compose only | none | Name of the database created at first start. |

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
| `--users` | `10` | Number of bot accounts |
| `--duration` | `60` | Seconds to run; `0` means until Ctrl+C |
| `--think-ms` | `200` | Average pause between actions |
| `--timeout` | `10` | Per-request timeout in seconds |
| `--seed` | none | Fix the random seed for repeatable runs |
| `--admin-key` | `$ADMIN_KEY` | If set, the bot checks the books at the end |

With `ADMIN_KEY` set in the bot's environment, it ends with a line like:

```
Books: minted 800000 cents, held 800000 cents -> BALANCED
```

`BALANCED` means every transfer was atomic. `MISMATCH` means the app has a bug. If no `Books:` line appears at all, the check did not run: the bot only checks the books when it has an admin key.

## Tests

```bash
pytest -q
```

The suite covers registration and login, password hashing, forged, expired, and unsigned tokens, transfer rules, idempotent retries, concurrent transfers that must not overdraw a wallet, history privacy and pagination, admin access, failure injection, and a complete bot run whose books must balance. It uses a throwaway SQLite database and its own secrets, so no environment setup is needed. The tests do not run against Postgres; that path was checked with the bot on the Compose stack. The same suite runs in CI on every pull request and push.

## Observability

`GET /metrics` exposes:

- `http_requests_total{method,path,status}` and `http_request_duration_seconds{method,path}`
- `wallet_registrations_total`
- `wallet_logins_total{result}`
- `wallet_transfers_total{result}` (`ok`, `insufficient_funds`, `unknown_recipient`, `self_transfer`)
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
- **Postgres row locking.** On Postgres, both wallets are locked in a fixed order so opposite transfers cannot deadlock. SQLite needs no row locks because it serializes writers. This path ran under concurrent bot load in the Compose stack and the books balanced.
- **Least privilege in the container.** The image runs as a non-root user, the application code is read-only to that user, and only the data folder is writable.
- **Least privilege in the pipeline.** The workflow's token is read-only by default, and only the publish job can write packages.
- **Database chosen by one variable.** The app reads only `DATABASE_URL`. Compose overrides it with a URL that points at the `db` service, so a value left in `.env` cannot silently send the app back to SQLite.

## Known limitations

These are deliberate gaps, and several are targets for later hardening work:

- Postgres is supported and verified in Docker Compose with 20 bot users, but the pytest suite (locally and in CI) still runs on SQLite, and the 50-user load test has not been run on Postgres.
- Postgres in Compose is slower than SQLite at 20 users (p95 about 350 ms against 156 ms). The cause is not measured yet.
- SQLite allows one writer at a time, which limits write throughput under heavy load.
- When run without Compose, the default SQLite file is lost when the container is removed.
- CI publishes images tagged by commit SHA only, with no vulnerability scanning and no image signing yet.
- GitHub Actions are pinned by major version tag, not by commit SHA.
- No rate limiting or lockout on login attempts.
- No token revocation or refresh tokens.
- No HTTPS in the app itself; TLS is expected to be terminated in front of it.
- `/metrics` is unauthenticated and should be restricted in any real deployment.
- Tables are created at startup. A real deployment would use migrations (for example Alembic).

## What broke

Problems I hit while building and testing this project are logged in [`BROKEN.md`](BROKEN.md): the symptom, how I found the cause, the fix, and the lesson. It includes wrong guesses and questions that are still open.

## Project structure

```
wallet-app/
├── .github/
│   └── workflows/
│       └── ci.yml       # test on every PR and push; publish image to GHCR from main
├── app/
│   ├── __init__.py
│   ├── database.py      # engine, sessions, SQLite/Postgres setup
│   ├── models.py        # User and Transaction tables
│   ├── security.py      # password hashing and JWT helpers
│   └── main.py          # API routes, metrics, failure injection
├── bot/
│   ├── __init__.py
│   └── bot.py           # simulated users used for filling up database 
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_wallet.py
│   └── test_bot.py
├── .dockerignore        # keeps .venv, data, .env, .git out of the image
├── .env.example
├── .gitignore
├── BROKEN.md            # log of real problems I hit and how I fixed them
├── docker-compose.yml   # app + Postgres, health check, persistent volume
├── Dockerfile           # multi-stage build, non-root user
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
└── README.md
```
