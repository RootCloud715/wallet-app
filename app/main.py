import asyncio
import hmac
import os
import random
import re
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from . import models, security
from .database import IS_SQLITE, Base, engine, get_db, wait_for_db
from .models import Transaction, User

SIGNUP_BONUS_CENTS = int(os.getenv("SIGNUP_BONUS_CENTS", "100000"))  # 1,000.00 mock money
MAX_TRANSFER_CENTS = 1_000_000  # 10,000.00 per transfer

# ---------------------------------------------------------------- metrics

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
REGISTRATIONS = Counter("wallet_registrations_total", "Accounts created")
LOGINS = Counter("wallet_logins_total", "Login attempts", ["result"])
TRANSFERS = Counter("wallet_transfers_total", "Transfer attempts", ["result"])
MONEY_MOVED = Counter("wallet_money_moved_cents_total", "Cents moved by successful transfers")

# ---------------------------------------------------------------- chaos
# Break-it switches for the practice drills. Off unless ENABLE_CHAOS=true, and
# changing them live needs the admin key.

chaos_state = {
    "latency_ms": int(os.getenv("CHAOS_LATENCY_MS", "0")),
    "error_rate": float(os.getenv("CHAOS_ERROR_RATE", "0")),
}
# Probes and scraping are never disturbed, so health checks stay honest.
CHAOS_EXEMPT = {"/health", "/ready", "/metrics"}


def chaos_enabled() -> bool:
    return os.getenv("ENABLE_CHAOS", "false").lower() == "true"


# ---------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(_: FastAPI):
    security.jwt_secret()  # fail at startup, not on the first login, if it is missing
    wait_for_db()
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Wallet API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def observe_and_inject_chaos(request: Request, call_next):
    path = request.url.path
    start = time.perf_counter()
    status = 500
    try:
        exempt = path in CHAOS_EXEMPT or path.startswith("/admin")
        if chaos_enabled() and not exempt:
            if chaos_state["latency_ms"] > 0:
                await asyncio.sleep(chaos_state["latency_ms"] / 1000)
            if random.random() < chaos_state["error_rate"]:
                status = 500
                return JSONResponse({"detail": "chaos: injected error"}, status_code=500)
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        if path != "/metrics":
            route = request.scope.get("route")
            label = route.path if route else "unmatched"
            REQUESTS.labels(request.method, label, str(status)).inc()
            LATENCY.labels(request.method, label).observe(time.perf_counter() - start)


# ---------------------------------------------------------------- schemas

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")


class Credentials(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def clean_username(cls, v: str) -> str:
        v = v.strip().lower()
        if not USERNAME_RE.match(v):
            raise ValueError("username must be 3-32 characters: letters, digits, underscore")
        return v


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = security.TOKEN_TTL_SECONDS


class MeOut(BaseModel):
    username: str
    balance_cents: int


class TransferIn(BaseModel):
    to: str
    amount_cents: int = Field(gt=0, le=MAX_TRANSFER_CENTS)

    @field_validator("to")
    @classmethod
    def clean_to(cls, v: str) -> str:
        return v.strip().lower()


class TransactionOut(BaseModel):
    id: int
    kind: str
    direction: Literal["in", "out"]
    from_user: str | None
    to_user: str
    amount_cents: int
    created_at: str


# ---------------------------------------------------------------- auth

bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=401,
        detail="invalid or missing token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise unauthorized
    user_id = security.decode_token(creds.credentials)
    if user_id is None:
        raise unauthorized
    user = db.get(User, user_id)
    if user is None:
        raise unauthorized
    return user


def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """Admin endpoints are off (404) unless ADMIN_KEY is set, then need that key."""
    expected = os.getenv("ADMIN_KEY", "")
    if not expected:
        raise HTTPException(status_code=404, detail="not found")
    if x_admin_key is None or not hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=403, detail="forbidden")


# ---------------------------------------------------------------- infra endpoints


@app.get("/health")
def health():
    """Liveness: the process is up. Does not touch the database."""
    return {"status": "ok"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)):
    """Readiness: the app can reach its database."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


class ChaosSettings(BaseModel):
    latency_ms: int = Field(default=0, ge=0, le=30_000)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)


@app.get("/admin/chaos", dependencies=[Depends(require_admin)])
def get_chaos():
    if not chaos_enabled():
        raise HTTPException(status_code=404, detail="not found")
    return chaos_state


@app.post("/admin/chaos", dependencies=[Depends(require_admin)])
def set_chaos(settings: ChaosSettings):
    if not chaos_enabled():
        raise HTTPException(status_code=404, detail="not found")
    chaos_state.update(settings.model_dump())
    return chaos_state


@app.get("/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats(db: Session = Depends(get_db)):
    """Totals for checking the books. Money must never be created or lost by transfers."""
    minted = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
            Transaction.kind == "signup_bonus"
        )
    )
    held = db.scalar(select(func.coalesce(func.sum(User.balance_cents), 0)))
    return {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "transactions": db.scalar(select(func.count(Transaction.id))) or 0,
        "money_minted_cents": int(minted),
        "money_held_cents": int(held),
        "books_balance": int(minted) == int(held),
    }


# ---------------------------------------------------------------- accounts


@app.post("/auth/register", response_model=MeOut, status_code=201)
def register(body: Credentials, db: Session = Depends(get_db)):
    user = User(
        username=body.username,
        password_hash=security.hash_password(body.password),
        balance_cents=SIGNUP_BONUS_CENTS,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="username already taken")
    db.add(
        Transaction(
            kind="signup_bonus", sender_id=None, receiver_id=user.id, amount_cents=SIGNUP_BONUS_CENTS
        )
    )
    db.commit()
    REGISTRATIONS.inc()
    return MeOut(username=user.username, balance_cents=user.balance_cents)


@app.post("/auth/login", response_model=TokenOut)
def login(body: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    stored = user.password_hash if user else security.DUMMY_HASH
    ok = security.verify_password(body.password, stored)
    if user is None or not ok:
        LOGINS.labels("failed").inc()
        # Same message for a wrong username and a wrong password, on purpose.
        raise HTTPException(status_code=401, detail="invalid username or password")
    LOGINS.labels("ok").inc()
    return TokenOut(access_token=security.create_token(user.id))


@app.get("/me", response_model=MeOut)
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    balance = db.scalar(select(User.balance_cents).where(User.id == user.id))
    return MeOut(username=user.username, balance_cents=int(balance))


# ---------------------------------------------------------------- transfers


def _lock_rows(db: Session, ids: list[int]) -> None:
    """Lock both wallets in a fixed order so two opposite transfers cannot deadlock.

    SQLite has no row locks (it serialises writers), so this only matters on Postgres.
    """
    if not IS_SQLITE:
        db.execute(select(User.id).where(User.id.in_(ids)).order_by(User.id).with_for_update())


def _to_out(db: Session, tx: Transaction, viewer_id: int) -> TransactionOut:
    sender = db.get(User, tx.sender_id) if tx.sender_id else None
    receiver = db.get(User, tx.receiver_id)
    return TransactionOut(
        id=tx.id,
        kind=tx.kind,
        direction="out" if tx.sender_id == viewer_id else "in",
        from_user=sender.username if sender else None,
        to_user=receiver.username,
        amount_cents=tx.amount_cents,
        created_at=tx.created_at.isoformat(),
    )


@app.post("/transfers", response_model=TransactionOut, status_code=201)
def transfer(
    body: TransferIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, max_length=64),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Send money. Send an Idempotency-Key header so a retried request cannot pay twice."""
    if idempotency_key:
        existing = db.scalar(
            select(Transaction).where(
                Transaction.sender_id == user.id, Transaction.idempotency_key == idempotency_key
            )
        )
        if existing:
            response.status_code = 200  # already done, here is the original result
            return _to_out(db, existing, user.id)

    receiver = db.scalar(select(User).where(User.username == body.to))
    if receiver is None:
        TRANSFERS.labels("unknown_recipient").inc()
        raise HTTPException(status_code=404, detail="recipient not found")
    if receiver.id == user.id:
        TRANSFERS.labels("self_transfer").inc()
        raise HTTPException(status_code=400, detail="cannot transfer to yourself")

    amount = body.amount_cents
    _lock_rows(db, [user.id, receiver.id])

    # One atomic statement checks the balance and takes the money. Two requests racing
    # on the same wallet cannot both pass, so the balance can never go negative.
    debited = db.execute(
        update(User)
        .where(User.id == user.id, User.balance_cents >= amount)
        .values(balance_cents=User.balance_cents - amount)
    )
    if debited.rowcount != 1:
        db.rollback()
        TRANSFERS.labels("insufficient_funds").inc()
        raise HTTPException(status_code=422, detail="insufficient funds")

    db.execute(
        update(User).where(User.id == receiver.id).values(balance_cents=User.balance_cents + amount)
    )
    tx = Transaction(
        kind="transfer",
        sender_id=user.id,
        receiver_id=receiver.id,
        amount_cents=amount,
        idempotency_key=idempotency_key,
    )
    db.add(tx)
    try:
        db.commit()
    except IntegrityError:
        # Two identical retries raced; the unique constraint let only one through.
        db.rollback()
        existing = db.scalar(
            select(Transaction).where(
                Transaction.sender_id == user.id, Transaction.idempotency_key == idempotency_key
            )
        )
        if existing is None:
            raise
        response.status_code = 200
        return _to_out(db, existing, user.id)

    TRANSFERS.labels("ok").inc()
    MONEY_MOVED.inc(amount)
    return _to_out(db, tx, user.id)


@app.get("/transactions", response_model=list[TransactionOut])
def history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    sender, receiver = aliased(User), aliased(User)
    rows = db.execute(
        select(Transaction, sender.username, receiver.username)
        .outerjoin(sender, sender.id == Transaction.sender_id)
        .join(receiver, receiver.id == Transaction.receiver_id)
        .where(or_(Transaction.sender_id == user.id, Transaction.receiver_id == user.id))
        .order_by(Transaction.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        TransactionOut(
            id=tx.id,
            kind=tx.kind,
            direction="out" if tx.sender_id == user.id else "in",
            from_user=from_name,
            to_user=to_name,
            amount_cents=tx.amount_cents,
            created_at=tx.created_at.isoformat(),
        )
        for tx, from_name, to_name in rows
    ]
