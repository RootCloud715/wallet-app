# What broke (and how I fixed it)

A log of real problems I hit while building and testing this project. Newest first. Each entry has the symptom, how I found the cause, the fix, and what I learned. Failures are more useful to me than successes, so they are all kept here, including wrong guesses.

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
- **The Postgres code path has not been run against a real Postgres yet.**
- **Why the WSL clock steps backwards is not investigated.** The app now tolerates it.
- **`StarletteDeprecationWarning` about `httpx` in the test client:** harmless for now, and Starlette is not pinned in `requirements.txt`.