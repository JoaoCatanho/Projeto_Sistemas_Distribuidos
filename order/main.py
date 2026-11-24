# test 1
import threading
from fastapi.responses import JSONResponse
from prometheus_client import Gauge
import random
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import json
import os
import pika
app = FastAPI(title="TicketMEI - Order")

CATALOG_URL = "http://catalog:8001/events"  # URL do serviço Catalog
PAYMENT_URL = "http://payment:8003/payments" # URL do payment
CB_STATE = Gauge("circuit_breaker_state", "0=Closed, 1=Open, 2=Half-Open")


MAX_TICKETS = 4  # limite de bilhetes por pedido (RF2)
MAX_TENTATIVAS = 3  # quantas vezes tentamos o Payment antes de desistir

ORDERS_INITIATED = Counter(
    "orders_initiated_total",
    "Total de pedidos criados",
    ["status"],
)  # contador de pedidos criados, com label status

PAYMENT_REQUEST_NUMBER = Counter("payment_client_requests_total", "Pedidos do Order ao Payment (cada tentativa conta)",
    ["status"],)

CB_FALLBACKS = Counter("circuit_breaker_fallbacks_total",
    "Compras recusadas logo porque o circuito estava aberto")

class OrderStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


# O que o cliente envia no POST /orders
class OrderRequest(BaseModel):
    costumer_id: str
    event_id: str
    tickets_quantity: int = Field(ge=1, le=MAX_TICKETS)  # o Pydantic valida sozinho (422)
    email: str


# A compra tal como fica guardada (campos do enunciado)
class Order(BaseModel):
    id: str
    costumer_id: str
    event_id: str
    tickets_quantity: int
    email: str
    total_amount: float
    created_at: str
    status: OrderStatus

class CircuitoAberto(Exception):
    """O circuito está aberto: nem tentamos chamar o Payment."""


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = 0, 1, 2

    def __init__(self, limite_falhas=5, tempo_aberto=15.0):
        self.limite_falhas = limite_falhas  # falhas seguidas até abrir
        self.tempo_aberto = tempo_aberto    # segundos em OPEN antes de testar
        self.falhas = 0
        self.aberto_em = 0.0
        self.lock = threading.Lock()
        self._mudar(self.CLOSED)

    def _mudar(self, novo):
        self.estado = novo
        CB_STATE.set(novo)  # o Prometheus vê sempre o estado atual

    def pode_chamar(self) -> bool:
        with self.lock:
            if self.estado == self.CLOSED:
                return True
            if self.estado == self.OPEN and time.monotonic() - self.aberto_em >= self.tempo_aberto:
                self._mudar(self.HALF_OPEN)  # deixa passar UM pedido de teste
                return True
            return False  # OPEN, ou HALF_OPEN com o teste já a decorrer

    def sucesso(self):
        with self.lock:
            self.falhas = 0
            self._mudar(self.CLOSED)

    def falha(self):
        with self.lock:
            self.falhas += 1
            if self.estado == self.HALF_OPEN or self.falhas >= self.limite_falhas:
                self.aberto_em = time.monotonic()
                self._mudar(self.OPEN)

cb = CircuitBreaker()


ORDERS: dict[str, Order] = {}  # compras guardadas em memória: {order_id: Order}

def chama_payment(order: Order) -> bool:
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        if not cb.pode_chamar():
            raise CircuitoAberto()   # nem chega a chamar o Payment
        if tenta_payment(order):
            cb.sucesso()
            return True
        cb.falha()
        if tentativa < MAX_TENTATIVAS:
            time.sleep(random.uniform(0, 0.1 * 2 ** tentativa))
    return False

def tenta_payment(order: Order) -> bool:
    try:
        r = httpx.post(
            PAYMENT_URL,
            json={"order_id": order.id, "amount": order.total_amount},
            timeout=2.0,
            )
    except httpx.TimeoutException:
        PAYMENT_REQUEST_NUMBER.labels(status="timeout").inc()
        return False
    except httpx.RequestError:
        PAYMENT_REQUEST_NUMBER.labels(status="error").inc()
        return False
    if r.status_code == 201:
        PAYMENT_REQUEST_NUMBER.labels(status="2xx").inc()
        return True
    PAYMENT_REQUEST_NUMBER.labels(status=str(r.status_code)).inc()
    return False

@app.post("/orders", status_code=201)

def create_order(req: OrderRequest):
    # 1. Perguntar o preço ao Catalog (comunicação síncrona)
    try:
        r = httpx.get(f"{CATALOG_URL}/{req.event_id}", timeout=3.0)
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Catalog indisponível")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Evento não encontrado")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Erro no Catalog")
    price = r.json()["price_eur"]

    # 2. Criar a compra no estado PENDING
    order_id = str(uuid.uuid4())
    order = Order(
        id=order_id,
        costumer_id=req.costumer_id,
        event_id=req.event_id,
        tickets_quantity=req.tickets_quantity,
        email=req.email,
        total_amount=price * req.tickets_quantity,
        created_at=datetime.now(timezone.utc).isoformat(),
        status=OrderStatus.PENDING,
    )
    ORDERS[order_id] = order

    # 3. Parte do payment
    if not chama_payment(order):
        order.status = OrderStatus.FAILED
        ORDERS_INITIATED.labels(status="FAILED").inc()
        raise HTTPException(status_code=502, detail="Pagamento falhou")
    order.status = OrderStatus.CONFIRMED

    # 4. (parte 4) enviar "OrderCreated" para o RabbitMQ
    try:
        publish_order_created(order)
    except (pika.exceptions.AMQPError, OSError) as exc:
        order.status = OrderStatus.FAILED
        ORDERS_INITIATED.labels(status="FAILED").inc()
        ORDERS[order_id] = order
        raise HTTPException(
            status_code=503,
            detail="Não foi possível enviar a compra para processamento",
        ) from exc
    ORDERS_INITIATED.labels(status="CONFIRMED").inc()
    return order


def publish_order_created(order: Order):
    parameters = pika.URLParameters(
        os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/ticketmei")
    )
    parameters.socket_timeout = 3
    parameters.blocked_connection_timeout = 3

    with pika.BlockingConnection(parameters) as connection:
        channel = connection.channel()

        channel.queue_declare(queue="orders.created", durable=True)
        channel.confirm_delivery()

        message = {
            "type": "OrderCreated",
            "order": order.model_dump(mode="json"),
        }

        channel.basic_publish(
            exchange="",
            routing_key="orders.created",
            body=json.dumps(message).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                message_id=order.id,
            ),
            mandatory=True,
        )


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    if order_id not in ORDERS:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    return ORDERS[order_id]

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)