import threading

import jwt

from tests.conftest import ADMIN


# ---------------------------------------------------------------- infra


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200


def test_metrics_exposed(client):
    client.get("/health")
    body = client.get("/metrics").text
    assert "http_requests_total" in body
    assert "wallet_transfers_total" in body


# ---------------------------------------------------------------- accounts


def test_register_gives_signup_bonus(client, make_user):
    _, auth = make_user()
    me = client.get("/me", headers=auth).json()
    assert me["balance_cents"] == 100000


def test_duplicate_username_is_409(client, make_user):
    name, _ = make_user()
    r = client.post("/auth/register", json={"username": name.upper(), "password": "another-password"})
    assert r.status_code == 409  # usernames are case-insensitive


def test_bad_registration_input_rejected(client):
    assert client.post("/auth/register", json={"username": "a", "password": "long-enough-pw"}).status_code == 422
    assert client.post("/auth/register", json={"username": "okname", "password": "short"}).status_code == 422
    assert client.post("/auth/register", json={"username": "bad name!", "password": "long-enough-pw"}).status_code == 422


def test_password_is_not_stored_in_plain_text(client, make_user):
    from app.database import SessionLocal
    from app.models import User

    name, _ = make_user("my-very-secret-pw")
    with SessionLocal() as db:
        stored = db.query(User).filter_by(username=name).one().password_hash
    assert "my-very-secret-pw" not in stored
    assert stored.startswith("scrypt$")


def test_login_failures_look_identical(client, make_user):
    name, _ = make_user()
    wrong_pw = client.post("/auth/login", json={"username": name, "password": "wrong-password-1"})
    no_user = client.post("/auth/login", json={"username": "nobody_here", "password": "wrong-password-1"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


# ---------------------------------------------------------------- auth on protected routes


def test_protected_routes_need_token(client):
    assert client.get("/me").status_code == 401
    assert client.get("/transactions").status_code == 401
    assert client.post("/transfers", json={"to": "x", "amount_cents": 1}).status_code == 401


def test_garbage_and_forged_tokens_rejected(client):
    assert client.get("/me", headers={"Authorization": "Bearer not.a.token"}).status_code == 401
    forged = jwt.encode({"sub": "1", "exp": 9999999999}, "attacker-guessed-secret-attacker-secret", algorithm="HS256")
    assert client.get("/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    unsigned = jwt.encode({"sub": "1", "exp": 9999999999}, key=None, algorithm="none")
    assert client.get("/me", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401


def test_expired_token_rejected(client, make_user):
    import os

    name, _ = make_user()
    expired = jwt.encode({"sub": "1", "exp": 1}, os.environ["JWT_SECRET"], algorithm="HS256")
    assert client.get("/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


# ---------------------------------------------------------------- transfers


def test_transfer_moves_money(client, make_user):
    alice, a = make_user()
    bob, b = make_user()
    r = client.post("/transfers", json={"to": bob, "amount_cents": 2500}, headers=a)
    assert r.status_code == 201
    assert r.json()["direction"] == "out"
    assert client.get("/me", headers=a).json()["balance_cents"] == 97500
    assert client.get("/me", headers=b).json()["balance_cents"] == 102500


def test_insufficient_funds(client, make_user):
    _, a = make_user()
    bob, _ = make_user()
    r = client.post("/transfers", json={"to": bob, "amount_cents": 100001}, headers=a)
    assert r.status_code == 422
    assert client.get("/me", headers=a).json()["balance_cents"] == 100000  # untouched


def test_transfer_validation(client, make_user):
    alice, a = make_user()
    bob, _ = make_user()
    assert client.post("/transfers", json={"to": alice, "amount_cents": 100}, headers=a).status_code == 400
    assert client.post("/transfers", json={"to": "ghost_user", "amount_cents": 100}, headers=a).status_code == 404
    assert client.post("/transfers", json={"to": bob, "amount_cents": 0}, headers=a).status_code == 422
    assert client.post("/transfers", json={"to": bob, "amount_cents": -5}, headers=a).status_code == 422
    assert client.post("/transfers", json={"to": bob, "amount_cents": 99_999_999}, headers=a).status_code == 422


def test_idempotency_key_prevents_double_payment(client, make_user):
    _, a = make_user()
    bob, b = make_user()
    h = {**a, "Idempotency-Key": "pay-invoice-42"}
    first = client.post("/transfers", json={"to": bob, "amount_cents": 1000}, headers=h)
    retry = client.post("/transfers", json={"to": bob, "amount_cents": 1000}, headers=h)
    assert first.status_code == 201
    assert retry.status_code == 200
    assert first.json()["id"] == retry.json()["id"]
    assert client.get("/me", headers=b).json()["balance_cents"] == 101000  # paid once


def test_concurrent_transfers_cannot_overdraw(client, make_user):
    """20 simultaneous requests of 30.00 against a 1,000.00 balance: at most 33 can succeed."""
    _, a = make_user()
    bob, b = make_user()
    results = []

    def fire():
        r = client.post("/transfers", json={"to": bob, "amount_cents": 30000}, headers=a)
        results.append(r.status_code)

    threads = [threading.Thread(target=fire) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    ok = results.count(201)
    assert ok == 3  # 3 x 300.00 = 900.00, a 4th would need 1,200.00
    assert results.count(422) == 17
    assert client.get("/me", headers=a).json()["balance_cents"] == 100000 - ok * 30000
    assert client.get("/me", headers=b).json()["balance_cents"] == 100000 + ok * 30000


# ---------------------------------------------------------------- history


def test_history_shows_both_sides(client, make_user):
    alice, a = make_user()
    bob, b = make_user()
    client.post("/transfers", json={"to": bob, "amount_cents": 700}, headers=a)

    a_hist = client.get("/transactions", headers=a).json()
    assert a_hist[0]["direction"] == "out" and a_hist[0]["to_user"] == bob
    assert a_hist[-1]["kind"] == "signup_bonus"  # oldest entry is the bonus

    b_hist = client.get("/transactions", headers=b).json()
    assert b_hist[0]["direction"] == "in" and b_hist[0]["from_user"] == alice


def test_history_is_private(client, make_user):
    alice, a = make_user()
    bob, b = make_user()
    carol, c = make_user()
    client.post("/transfers", json={"to": bob, "amount_cents": 700}, headers=a)
    carol_hist = client.get("/transactions", headers=c).json()
    assert all(t["to_user"] == carol for t in carol_hist)  # only her own bonus


def test_history_pagination(client, make_user):
    _, a = make_user()
    bob, _ = make_user()
    for _ in range(5):
        client.post("/transfers", json={"to": bob, "amount_cents": 100}, headers=a)
    assert len(client.get("/transactions?limit=2", headers=a).json()) == 2
    assert len(client.get("/transactions?limit=100&offset=3", headers=a).json()) == 3  # 6 total


# ---------------------------------------------------------------- admin and chaos


def test_admin_needs_key(client):
    assert client.get("/admin/stats").status_code == 403
    assert client.get("/admin/stats", headers={"X-Admin-Key": "wrong"}).status_code == 403
    assert client.get("/admin/stats", headers=ADMIN).status_code == 200


def test_books_balance_after_activity(client, make_user):
    _, a = make_user()
    bob, _ = make_user()
    client.post("/transfers", json={"to": bob, "amount_cents": 1234}, headers=a)
    stats = client.get("/admin/stats", headers=ADMIN).json()
    assert stats["books_balance"] is True
    assert stats["money_minted_cents"] == stats["money_held_cents"]


def test_chaos_off_by_default(client):
    assert client.post("/admin/chaos", json={"latency_ms": 100}, headers=ADMIN).status_code == 404


def test_chaos_error_injection(client, make_user, monkeypatch):
    _, a = make_user()
    monkeypatch.setenv("ENABLE_CHAOS", "true")
    assert client.post("/admin/chaos", json={"error_rate": 1.0}, headers=ADMIN).status_code == 200

    assert client.get("/me", headers=a).status_code == 500  # injected failure
    assert client.get("/health").status_code == 200  # probes stay exempt
    assert client.get("/metrics").status_code == 200

    client.post("/admin/chaos", json={"error_rate": 0}, headers=ADMIN)
    assert client.get("/me", headers=a).status_code == 200
