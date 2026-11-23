# What broke (and how I fixed it)

A log of real problems I hit while building and testing this project. Newest first. Each entry has the symptom, how I found the cause, the fix, and what I learned. Failures are more useful to me than successes, so they are all kept here, including wrong guesses.

---

## 2026-11-24: CI failed with `pytest: command not found` (exit code 127)

- **Symptom:** the `test` job went red in about 10 seconds at the `Run pytest` step. The log said `pytest: command not found` and `Process completed with exit code 127`. The earlier steps (checkout, Python setup, `pip install`) had all passed.
- **Root cause:** a CI runner starts empty and gets only what the workflow installs. My workflow ran `pip install -r requirements.txt`, but pytest is a test-only dependency that lives in `requirements-dev.txt`. On my laptop the venv already had it, so the problem was invisible locally.
- **Fix:** added a step that also installs `requirements-dev.txt` before `pytest`. Keeping test tools out of `requirements.txt` is deliberate, because the Docker image installs `requirements.txt` and should not ship pytest. After the fix, `test` passed in 47 seconds.
- **Lesson:** exit code 127 means "command not found", which points at the environment, not at the code. "Works on my machine" usually means my machine has something the pipeline was never told to install. A pipeline that starts from a blank machine is a reproducibility test.

---

## 2026-11-24: CI failed at `setup-python`: Python 3.13.1 not found on Ubuntu 26.04

- **Symptom:** the first real run of `test` failed after 6 seconds. That was too fast for installing dependencies, which pointed at the setup steps. The log of `actions/setup-python` said: `The version '3.13.1' with architecture 'x64' was not found for Ubuntu 26.04`.
- **Root cause:** `ubuntu-latest` now resolves to Ubuntu 26.04, and `setup-python` can only install versions that GitHub has built for that image. The exact patch `3.13.1` is not in that list. My Dockerfile pins `3.13.1`, and I had copied the same number into the workflow, but the two systems get Python from different places: Docker Hub for the image, and the runner's prebuilt list for CI.
- **Fix:** changed the workflow to `python-version: "3.13"`, which takes the newest 3.13 patch available on the runner. The Dockerfile keeps its exact pin. The only mismatch is that pytest in CI runs on a slightly newer patch than the image, which is harmless here.
- **Also learned:** my first draft had `3.13.1-slim` in this field. `-slim` is a Docker image tag, not a Python version, so it would have failed even with the right OS. Quote the version in YAML, because an unquoted `3.10` is read as the number `3.1`.
- **Lesson:** a 6-second failure is a setup failure. Read the first red step, not the last. Do not assume that a version pinned in one place exists in another. Also, `ubuntu-latest` is a moving target, so the runner image can change under a pipeline that did not change.

---

## 2026-11-24: First push of the workflow was rejected (token missing `workflow` scope)

- **Symptom:** `git push -u origin add-ci` ended with `[remote rejected] ... refusing to allow a Personal Access Token to create or update workflow .github/workflows/ci.yml without workflow scope`. The commit itself was fine and was saved locally.
- **Root cause:** GitHub treats workflow files as special, because a token that can edit them can make GitHub run arbitrary code in the repository. A personal access token needs the `workflow` permission (classic token) or **Workflows: Read and write** (fine-grained token) before it can push anything under `.github/workflows/`. My token had repository access but not that.
- **Fix:** added the workflow permission to the token in GitHub's developer settings and pushed again. The commit did not need to be redone.
- **Lesson:** a rejected push does not mean the commit is lost. Read the remote message: it named the exact missing permission. Tokens should have only the permissions they need, so the extra scope was a deliberate, narrow addition. The token string never goes into chat, files, or the repo.

---

## 2026-11-24: Mistakes in my first CI workflow drafts (caught in review before they ran)

None of these reached GitHub, but they are the mistakes a beginner is likely to make, so I am keeping them.

- **Missing versions:** `uses: actions/checkout` with no `@version`. Every action needs a version, and pinning it is a supply-chain control.
- **Typos that fail silently or late:** `requirement.txt` for `requirements.txt`, and `JWT_SECRETS` for `JWT_SECRET`. The second one would not crash anything. The app would just never see the variable, and the tests would fail later with a confusing error. Names must match exactly what the code reads, so I checked them with `grep`.
- **Structure errors in the publish job:** the job name was missing or at the wrong indent, `needs` and `if` sat at different depths, the actions were written as keys (`docker/login-action:`) instead of steps (`- uses: docker/login-action@...` with a `with:` block), and `runs-on` and a checkout step were missing.
- **Wrong key:** `content: read` instead of `contents: read`.
- **YAML syntax:** a stray colon after `docker/build-push-action@v7:` and `push: true` indented five spaces instead of ten. YAML indentation is the structure, and tabs are not allowed.
- **Real values in a public file:** the `<owner>` placeholder had to become my lowercase username, and secrets must never be pasted into a workflow file. The values in the `test` step are throwaway strings that only let the tests start.
- **Lesson:** read the workflow top to bottom as a machine would, and check the structure before checking the logic. Reading the log of a failed run is faster than guessing, but a careful read before pushing saves a round trip.

---

## 2026-11-22: Postgres in Compose is slower than SQLite (cause not measured)

This is a finding rather than a crash, and I do not know the cause yet.

- **Symptom:** the same bot command (`--users 20 --duration 120`) gave different numbers on the two databases. A 60-second run on Postgres gave similar numbers (54.6 requests per second, p95 327 ms).

  | | SQLite (earlier run) | Postgres in Compose |
  |---|---|---|
  | Requests in 120 s | 9,677 (about 80 per second) | 6,282 and 6,307 (about 52 per second) |
  | p95 latency | 156 ms | 359 ms and 349 ms |
  | Server errors (5xx) | none | none |

- **Caveat:** this is not a controlled comparison. The SQLite run was earlier and outside Docker, and the bot, the app, and Postgres all share one laptop.
- **Candidate causes (none measured):** the extra network hop between containers, a single uvicorn worker, password hashing using the CPU, the bot competing with the stack for the same machine, and untuned Postgres defaults.
- **Plan:** compare average latency per endpoint from `/metrics`, and run the 50-user test on Postgres, since the unexplained 50-user slowdown below may be the same bottleneck.
- **Lesson:** write the numbers down before explaining them. "Slower" is a measurement, and the cause is a separate question that needs its own test.

---

## 2026-11-22: The bot's books check was silently skipped (no `BALANCED` line)

- **Symptom:** three bot runs against the Compose stack finished with no errors, but none printed the final `Books: ... BALANCED` line. I had expected that line.
- **How I found the cause:** `grep -rniE "balanced|admin_key" bot/ README.md` showed that the bot only checks the books if it receives an admin key, from `--admin-key` or the `ADMIN_KEY` environment variable (`bot/bot.py` lines 185 and 206). The app container had the key from `.env`, but my host shell did not, so the bot skipped the check without any warning.
- **Fix:** load the key into the shell without printing it: `export ADMIN_KEY=$(grep '^ADMIN_KEY=' .env | cut -d= -f2-)`. Then `python bot/bot.py --users 20 --duration 60 --base-url http://localhost:8000` ended with `Books: minted 6500000 cents, held 6500000 cents -> BALANCED`.
- **Check:** 65 accounts (5 + 20 + 20 + 20 bots) times the 100,000-cent signup bonus is exactly 6,500,000 cents, so the books matched every transfer from all runs since the stack started, including the concurrent ones that exercised the Postgres row locks.
- **Lesson:** a check that is skipped looks the same as a check that passed. A missing success line means the check did not run, not that nothing failed.

---

## 2026-11-22: Compose built the wrong project (`Docker-compose.yml` with a capital D)

- **Symptom:** `docker compose up --build` failed at `COPY requirements.txt .` with `"/requirements.txt": not found`. The error showed a short single-stage Dockerfile and an image named `student-api`, although my Dockerfile is multi-stage and the project folder is `wallet-app`.
- **How I found the cause:** `docker compose config | grep -nE "name:|context:|dockerfile:|image:"` printed `name: student` and `context: /home/student`. So Compose was not using the file in `~/wallet-app` at all. `ls -la` then showed my file was named `Docker-compose.yml`.
- **Root cause:** Compose only recognizes `compose.yaml`, `compose.yml`, `docker-compose.yaml`, and `docker-compose.yml`, and Linux filenames are case-sensitive. Compose did not see my file, searched parent folders, and used a compose file in my home folder, which has a different build context.
- **Fix:** `mv Docker-compose.yml docker-compose.yml`. After that the build used `/home/student/wallet-app`, the image was named `wallet-app-app`, and the multi-stage Dockerfile was used.
- **Lesson:** when a build does something that makes no sense, check what Compose actually resolved with `docker compose config` before changing any code. Also, Compose silently walks up to parent folders, so a stray compose file elsewhere can be picked up.

---

## 2026-11-22: The first Compose draft would have used SQLite without telling me

I caught this in review before running it.

- **What was wrong:** the draft set `DB_HOST: db` for the app, but the app never reads that. It reads only `DATABASE_URL` (`app/database.py`), which defaults to `sqlite:///./data/wallet.db`. The stack would have started, the bot would have worked, and Postgres would have sat idle. The `.env.example` also contained the SQLite `DATABASE_URL`, and `env_file: .env` would have passed it straight into the container. The draft also had a hardcoded `POSTGRES_PASSWORD: secret` in a committed file, and the port mapping `8000:5000` did not match uvicorn, which listens on 8000.
- **Fix:**
  - The `app` service sets `DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}` under `environment:`, which overrides `env_file:`.
  - The `POSTGRES_*` values come from the git-ignored `.env`, and `.env.example` lists them as placeholders.
  - The SQLite `DATABASE_URL` line in `.env.example` is commented out, because Compose supplies the URL.
  - The ports are `8000:8000`.
- **Proof that it is really Postgres:** `docker compose exec db psql -U <user> -d <db> -c '\dt'` listed the `users` and `transactions` tables, and the user count rose to 45 after three bot runs (5 + 20 + 20). The count was 45 again after `docker compose down` and `up -d`, so the named volume persists the data.
- **Lesson:** a working app does not prove which database it is using. Check the database itself. The `psycopg` URL rewrite in `app/database.py` means a plain `postgresql://` URL works.

---

## 2026-11-17: Container exits immediately as non-root (`PermissionError: /app/data`)

This one was a deliberate drill, not an accident. I had a working non-root Dockerfile and removed the `mkdir`/`chown` line on purpose to see the failure and learn to diagnose it.

- **Symptom:** `docker ps -a` showed the container as `Exited (1)` about one second after it started. Exit code 1 (confirmed with `docker inspect`). `/health` never had a chance to answer.
- **How I found the cause:** `docker logs` showed a long traceback. The last line names the error: `PermissionError: [Errno 13] Permission denied: '/app/data'`. Reading upward, the first path in my own code was `/app/app/database.py`, line 22, the `os.makedirs(...)` call. Everything above it is uvicorn and importlib internals. To prove the cause I ran two throwaway containers that override the start command:
  - `docker run --rm wallet-app:broken ls -ld /app` printed `drwxr-xr-x ... root root /app`
  - `docker run --rm wallet-app:broken id` printed `uid=10001(appuser) gid=10001(appuser)`
- **Root cause:** `WORKDIR /app` creates the folder owned by root with mode `755`, so only root can write to it. The app runs as `appuser` (uid 10001), which counts as "other" and has no write permission. `database.py` creates `data/` for the SQLite file at import time, so the app crashed during import, before uvicorn could start listening (which is why the logs have no "Uvicorn running" line).
- **Fix:** add `RUN mkdir -p /app/data && chown -R appuser:appuser /app/data` before the `USER appuser` line. It must come before `USER`, because after the switch `appuser` is not allowed to change ownership of anything. I only chown `data/`, not all of `/app`, so the app code stays read-only to the app.
- **Check after the fix:** `/health` returns `{"status":"ok"}`, `whoami` prints `appuser`, `id` shows uid 10001, and `touch /app/app/text.txt` fails with `Permission denied`, so a compromised process could not modify the code.
- **Lesson:** read a traceback from the bottom (the error) and then find the first line that is my own code. Running as non-root means every path the app writes to must be made writable on purpose. The same class of problem comes back with mounted volumes in Docker Compose.

---

## 2026-11-14: Random 401 errors in the test suite (clock skew)

- **Symptom:** about 4 in 10 full test runs failed (7 of 17), always with `401 Unauthorized` on a token that had just been issued. A different test failed each time.
- **First guess (wrong):** a leftover shared database. That was a real bug (next entry), but fixing it did not stop the random failures.
- **How I found it:** I wrapped `jwt.decode` to record the exact exception behind every rejected token. It logged `ImmatureSignatureError: The token is not yet valid (iat)` 19 times. A separate loop showed the WSL clock going backwards several times during a test run.
- **Root cause:** each token records the second it was issued. When the clock stepped backwards between login and the next request, a fresh token looked like it came from the future and was rejected. The most likely source is the WSL2 virtual machine's clock, which is re-synced with Windows. I did not investigate why the clock jitters.
- **Fix:** added `leeway=10` to token validation in `app/security.py`, the standard setting for clock skew between machines. Ten full test runs in a row passed afterwards.
- **Lesson:** measure a failure instead of guessing. Clocks are never perfectly in sync, which will matter again with several servers or Kubernetes pods. The cost of the fix is that an expired token stays valid for up to 10 extra seconds.

---

## 2026-11-14: Tests silently used the real database

- **Symptom:** the second test run failed with `409` when registering `user_1`, although the first run passed.
- **How I found it:** printing `database.DATABASE_URL` after loading the test setup showed `./data/wallet.db`, the real database, instead of a throwaway one in `/tmp`.
- **Root cause:** in `tests/conftest.py` the line importing the app had been moved above the code that points the tests at a temporary database (probably an editor's organize-imports on save). The app loaded first and used the default path. The first run on an empty database passed, which is also why a fresh clone on Windows looked fine.
- **Fix:** restored the order, added an `# isort: off` fence so editors leave it alone, and added an `assert` in `conftest.py` that fails immediately if the temporary database is not in use.
- **Lesson:** a silent misconfiguration should become a loud failure. Tests must never touch real data.

---

## 2026-11-14: `ModuleNotFoundError: No module named 'fastapi'` in WSL

- **Symptom:** `uvicorn app.main:app` crashed on import, and the traceback pointed at `/usr/bin/uvicorn`.
- **Root cause:** the command ran the system-wide `uvicorn` installed outside any virtual environment, which has no project packages. The venv was not created or not active.
- **Fix:** created and activated a venv, installed `requirements-dev.txt`, and checked with `which uvicorn` that it points into `.venv/`. Running `python -m uvicorn` always uses the active interpreter.
- **Lesson:** check which binary actually runs (`which`) before debugging the code.

---

## 2026-11-14: `pip install` timed out in WSL

- **Symptom:** `ReadTimeoutError` from `files.pythonhosted.org` while downloading SQLAlchemy, at about 23 kB/s.
- **How I found it:** a quick `curl` speed test against PyPI showed about 181 kB/s, so the connection worked and the slowdown was temporary.
- **Fix:** `pip install --default-timeout=120 --retries 10 -r requirements-dev.txt`. pip keeps finished downloads in its cache, so a retry has less to fetch.
- **Lesson:** not every error is a code error. Read the exception type before changing anything.

---

## 2026-10-31: Virtual environment broke after moving the project folder

- **Symptom:** `Fatal error in launcher: Unable to create process using ...` when running `uvicorn` right after moving the project out of OneDrive.
- **Root cause:** a venv stores absolute paths from where it was created, so a moved venv cannot work.
- **Fix:** deleted `.venv`, recreated it, and reinstalled from `requirements.txt`.
- **Lesson:** a venv is disposable and must be rebuilt, never moved. This is what `requirements.txt` is for.

---

## 2026-10-31: Wrong guess about slowdown under load

- **Symptom:** with 20 bot users the API handled about 80 requests per second (p95 156 ms). With 50 bot users, throughput fell to about 57 per second and p95 rose to about 1,800 ms, with no errors.
- **Hypothesis:** the project folder was inside OneDrive, and syncing the SQLite files was slowing it down.
- **Test:** moved the project out of OneDrive and repeated the 50-user run.
- **Result:** the hypothesis was wrong. Throughput stayed at 56.8 per second both times, and p50 was 463 ms before and 469 ms after.
- **Lesson:** test a guess before trusting it. An identical result in both runs suggests one resource is saturated.

---

## Still open

- **Cause of the slowdown at 50 bot users is not measured, and the 50-user test has not been run on Postgres yet.** Candidates are password hashing (CPU), SQLite allowing only one writer at a time, or the bot and server sharing one laptop. Postgres at 20 users is also slower than SQLite (see the 2026-11-22 entry), so these may share a cause. Plan: compare average latency per endpoint from `/metrics`, then run 50 users against Postgres.
- **Why the WSL clock steps backwards is not investigated.** The app now tolerates it.
- **`StarletteDeprecationWarning` about `httpx` in the test client:** harmless for now, and Starlette is not pinned in `requirements.txt`.
- **Docker image size, multi-stage did not shrink it:** the single-stage non-root image is 318 MB on disk (74.7 MB compressed) and the multi-stage image is 322 MB (75.2 MB), about 4 MB larger. The image built by CI measures the same as my local build (322 MB, 75.2 MB), so the difference is not caused by my machine. I predicted it would grow by about 50 MB, so I was off by roughly 12 times: stage 1 had almost nothing to throw away (a tiny `requirements.txt`, `--no-cache-dir`, and `psycopg[binary]` ships a prebuilt wheel, so no compiler). One likely cause is that `python -m venv` adds its own copy of pip (13 MB, measured with `du -sh`) on top of the one in the base image. That alone should have made it about 13 MB larger, not 4 MB, so the gap is not fully explained. Next step: compare `docker history` for both images layer by layer. Lesson: multi-stage pays off when the build stage needs compilers or headers that the runtime does not.
- **GitHub Actions are pinned by major version tag (for example `@v7`), not by commit SHA.** A tag can be moved by the action's author, so pinning each action to a full commit SHA, with the version in a comment, is the stricter option. Planned as a later hardening step.

## Resolved from this list

- **The Postgres code path now runs against a real Postgres.** Postgres 16 in Docker Compose, 20 bot users, ended with `BALANCED`. The row-locking code in `_lock_rows` ran under concurrent transfers without errors or deadlocks. The volume held its data across `docker compose down` and `up -d`, and I saw no permission problems with it.
