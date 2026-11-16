# What broke (and how I fixed it)

A log of real problems I hit while building and testing this project. Newest first. Each entry has the symptom, how I found the cause, the fix, and what I learned. Failures are more useful to me than successes, so they are all kept here, including wrong guesses.

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

- **Cause of the slowdown at 50 bot users is not measured.** Candidates are password hashing (CPU), SQLite allowing only one writer at a time, or the bot and server sharing one laptop. Plan: compare average latency per endpoint from `/metrics`, and re-test on Postgres.
- **The Postgres code path has not been run against a real Postgres yet.** In Docker Compose, check volume permissions for the database container.
- **Why the WSL clock steps backwards is not investigated.** The app now tolerates it.
- **`StarletteDeprecationWarning` about `httpx` in the test client:** harmless for now, and Starlette is not pinned in `requirements.txt`.
- **Docker image size, multi-stage did not shrink it:** the single-stage non-root image is 318 MB on disk (74.7 MB compressed) and the multi-stage image is 322 MB (75.2 MB), about 4 MB larger. I predicted it would grow by about 50 MB, so I was off by roughly 12 times: stage 1 had almost nothing to throw away (a tiny `requirements.txt`, `--no-cache-dir`, and `psycopg[binary]` ships a prebuilt wheel, so no compiler). One likely cause is that `python -m venv` adds its own copy of pip (13 MB, measured with `du -sh`) on top of the one in the base image. That alone should have made it about 13 MB larger, not 4 MB, so the gap is not fully explained. Next step: compare `docker history` for both images layer by layer. Lesson: multi-stage pays off when the build stage needs compilers or headers that the runtime does not.
