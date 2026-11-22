import asyncio
import os
import random
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field


PAYMENT_REQUESTS = Counter(
    "payment_requests_total", "Pedidos de pagamento por código HTTP", ["http_status"]
)
PAYMENT_DURATION = Histogram(
    "payment_processing_duration_seconds", "Duração dos pedidos de pagamento"
)


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)
    amount: float = Field(gt=0, allow_inf_nan=False)


class FaultConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_rate: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    latency_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)


class Payment(BaseModel):
    id: str
    order_id: str
    amount: float
    status: str
    processed_at: str


@contextmanager
def connect(database_path: Path):
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(database_path: Path):
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE,
                amount REAL NOT NULL CHECK (amount > 0),
                status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
                processed_at TEXT NOT NULL
            )"""
        )


def find_payment(database_path: Path, request: PaymentRequest):
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM payments WHERE order_id = ?", (request.order_id,)
        ).fetchone()
    if row is not None:
        if row["amount"] != request.amount:
            raise HTTPException(409, "Já existe um pagamento com outro valor para esta compra")
        return Payment(**dict(row))
    return None


def save_payment(database_path: Path, request: PaymentRequest, failed: bool):
    # Serializar a verificação e a escrita: pedidos concorrentes não podem
    # duplicar uma cobrança nem substituir um SUCCESS por FAILED.
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM payments WHERE order_id = ?", (request.order_id,)
        ).fetchone()
        if row is not None:
            if row["amount"] != request.amount:
                raise HTTPException(409, "Já existe um pagamento com outro valor para esta compra")
            if row["status"] == "SUCCESS":
                return Payment(**dict(row))

        payment = Payment(
            id=row["id"] if row is not None else str(uuid.uuid4()),
            order_id=request.order_id,
            amount=request.amount,
            status="FAILED" if failed else "SUCCESS",
            processed_at=datetime.now(timezone.utc).isoformat(),
        )
        connection.execute(
            """INSERT INTO payments (id, order_id, amount, status, processed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET
                   status = excluded.status, processed_at = excluded.processed_at""",
            (payment.id, payment.order_id, payment.amount, payment.status, payment.processed_at),
        )
    return payment


def create_app(database_path: Path | None = None):
    database_path = database_path or Path(
        os.getenv("PAYMENT_DB_PATH", str(Path(__file__).parent / "data" / "payments.db"))
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(initialize_database, database_path)
        app.state.faults = FaultConfiguration()
        yield

    app = FastAPI(title="TicketMEI - Payment", lifespan=lifespan)

    @app.middleware("http")
    async def measure_payment(request: Request, call_next):
        if request.method != "POST" or request.url.path != "/payments":
            return await call_next(request)
        started_at = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            PAYMENT_REQUESTS.labels(http_status=str(status_code)).inc()
            PAYMENT_DURATION.observe(time.perf_counter() - started_at)

    @app.post("/payments", status_code=201, response_model=Payment,
              responses={500: {"model": Payment}})
    async def create_payment(request: PaymentRequest):
        existing = await asyncio.to_thread(find_payment, database_path, request)
        if existing is not None and existing.status == "SUCCESS":
            return existing

        # A configuração é capturada por pedido. O atraso não bloqueia outros
        # pagamentos, a configuração de falhas nem o scraping de métricas.
        faults = app.state.faults
        await asyncio.sleep(faults.latency_seconds)
        failed = random.random() < faults.error_rate
        payment = await asyncio.to_thread(save_payment, database_path, request, failed)
        if payment.status == "FAILED":
            return JSONResponse(status_code=500, content=payment.model_dump())
        return payment

    @app.post("/payments/configure-faults", response_model=FaultConfiguration)
    async def configure_faults(configuration: FaultConfiguration):
        # Substituir a configuração completa; {} restaura o funcionamento normal.
        app.state.faults = configuration
        return configuration

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), headers={"content-type": CONTENT_TYPE_LATEST})

    return app


app = create_app()
