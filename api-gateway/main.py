import os
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Reuse the connection pool across requests and close it on shutdown.
    async with (
        httpx.AsyncClient(
            base_url=os.getenv("CATALOG_URL", "http://catalog:8001"),
            timeout=httpx.Timeout(5.0, connect=1.0),
            trust_env=False,
        ) as catalog_client,
        httpx.AsyncClient(
            base_url=os.getenv("ORDER_URL", "http://order:8002"),
            timeout=httpx.Timeout(5.0, connect=1.0),
            trust_env=False,
        ) as order_client,
    ):
        app.state.catalog_client = catalog_client
        app.state.order_client = order_client
        yield


app = FastAPI(title="TicketMei API Gateway", lifespan=lifespan)

REQUESTS = Counter(
    "gateway_requests_total",
    "Requests to the API gateway",
    ["method", "endpoint", "http_status"],
)
LATENCY = Histogram(
    "gateway_request_duration_seconds", "Gateway request duration", ["endpoint"]
)


@app.middleware("http")
async def measure_request(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)

    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        # Group event and order IDs under their route patterns.
        endpoint = route.path if route else "unmatched"
        LATENCY.labels(endpoint=endpoint).observe(time.perf_counter() - start)
        REQUESTS.labels(
            method=request.method, endpoint=endpoint, http_status=status_code
        ).inc()


async def forward_request(
    request: Request, client: httpx.AsyncClient, path: str, service_name: str
) -> Response:
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]
    try:
        upstream = await client.request(
            method=request.method,
            url=path,
            params=request.query_params.multi_items(),
            content=await request.body(),
            headers=headers,
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504, detail=f"{service_name} request timed out"
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"{service_name} service unavailable"
        ) from exc

    # Preserve upstream errors (such as 404/422) and successful responses (202).
    headers = {}
    if "content-type" in upstream.headers:
        headers["content-type"] = upstream.headers["content-type"]
    return Response(
        content=upstream.content, status_code=upstream.status_code, headers=headers
    )


async def forward_to_catalog(request: Request, path: str) -> Response:
    return await forward_request(
        request, request.app.state.catalog_client, path, "Catalog"
    )


async def forward_to_order(request: Request, path: str) -> Response:
    return await forward_request(request, request.app.state.order_client, path, "Order")


@app.get("/events")
async def list_events(request: Request):
    return await forward_to_catalog(request, "/events")


@app.get("/events/{event_id}")
async def get_event(event_id: str, request: Request):
    return await forward_to_catalog(request, f"/events/{quote(event_id, safe='')}")


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), headers={"content-type": CONTENT_TYPE_LATEST})


@app.post("/orders", status_code=202)
async def create_order(
    request: Request,
    body: dict = Body(
        ...,
        examples=[
            {
                "costumer_id": "cliente-001",
                "event_id": "evt-101",
                "tickets_quantity": 2,
                "email": "cliente@example.com",
            }
        ],
    ),
):
    return await forward_to_order(request, "/orders")


@app.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request):
    return await forward_to_order(request, f"/orders/{quote(order_id, safe='')}")
