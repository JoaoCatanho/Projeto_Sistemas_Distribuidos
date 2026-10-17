import os
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Reuse the connection pool across requests and close it on shutdown.
    async with httpx.AsyncClient(
        base_url=os.getenv("CATALOG_URL", "http://catalog:8001"),
        timeout=httpx.Timeout(5.0, connect=1.0),
        trust_env=False,
    ) as client:
        app.state.catalog_client = client
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
        # Group IDs under /events/{event_id}; avoid unbounded metric labels.
        endpoint = route.path if route else "unmatched"
        LATENCY.labels(endpoint=endpoint).observe(time.perf_counter() - start)
        REQUESTS.labels(
            method=request.method, endpoint=endpoint, http_status=status_code
        ).inc()


async def forward_to_catalog(request: Request, path: str) -> Response:
    try:
        upstream = await request.app.state.catalog_client.get(
            path, params=request.query_params.multi_items()
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Catalog request timed out") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Catalog service unavailable") from exc

    # Preserve catalog errors (such as 404) as well as successful responses.
    headers = {}
    if "content-type" in upstream.headers:
        headers["content-type"] = upstream.headers["content-type"]
    return Response(
        content=upstream.content, status_code=upstream.status_code, headers=headers
    )


@app.get("/events")
async def list_events(request: Request):
    return await forward_to_catalog(request, "/events")


@app.get("/events/{event_id}")
async def get_event(event_id: str, request: Request):
    return await forward_to_catalog(request, f"/events/{quote(event_id, safe='')}")


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), headers={"content-type": CONTENT_TYPE_LATEST})
