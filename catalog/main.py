import json
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException , Request, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = FastAPI()

REQUESTS = Counter("catalog_requests_total", "Pedidos ao Catalog",
                   ["endpoint", "http_status"])

LATENCY = Histogram("catalog_request_duration_seconds", "Duração dos pedidos")

@app.middleware("http")
async def medir(request: Request, call_next):
    if request.url.path == "/metrics":      # não contar as visitas do Prometheus
        return await call_next(request)
    inicio = time.perf_counter()            # liga o cronómetro
    response = await call_next(request)     # deixa a rota fazer o trabalho
    LATENCY.observe(time.perf_counter() - inicio)
    rota = request.scope.get("route")       # "/events/{event_id}" em vez de "/events/evt-102"
    endpoint = rota.path if rota else request.url.path
    REQUESTS.labels(endpoint=endpoint, http_status=response.status_code).inc()
    return response

EVENTS_FILE = Path(__file__).parent / "data" / "events.json"
with open(EVENTS_FILE, encoding="utf-8") as f:
    EVENTS = json.load(f)

@app.get("/events")
def list_events():
    return EVENTS

# GET /events/evt-102 -> devolve só esse evento
@app.get("/events/{event_id}")
def get_event(event_id: str):
    for event in EVENTS:
        if event["id"] == event_id:
            return event
    # Se o ciclo acabou sem encontrar nada:
    raise HTTPException(status_code=404, detail="Evento não encontrado")

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)