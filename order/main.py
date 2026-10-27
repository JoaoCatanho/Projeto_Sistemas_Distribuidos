import uuid
from datetime import datetime, timezone
from enum import Enum

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="TicketMEI - Order")

CATALOG_URL = "http://catalog:8001/events"  # URL do serviço Catalog

MAX_TICKETS = 4  # limite de bilhetes por pedido (RF2)


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


ORDERS: dict[str, Order] = {}  # compras guardadas em memória: {order_id: Order}


@app.post("/orders", status_code=202)
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

    # 3. (parte 3) enviar "OrderCreated" para o RabbitMQ

    return order


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    if order_id not in ORDERS:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    return ORDERS[order_id]