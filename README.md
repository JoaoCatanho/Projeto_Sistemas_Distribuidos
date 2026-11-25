# TicketMEI – Plataforma de venda de bilhetes para eventos

Projeto da unidade curricular de **Sistemas Distribuídos** (MEI, Universidade da Madeira, 2026/2027).

| Entrega | O que inclui |
|---|---|
| Parte I, Fase 1 | API Gateway e Catalog, Docker Compose, Prometheus, Grafana, k6 |
| Parte I, Fase 2 | Order, Notification e RabbitMQ (comunicação assíncrona com `OrderCreated`) |
| Parte I, Fase 3 | Kubernetes, CI/CD com GitHub Actions + Argo CD, rate limiter com Redis no Gateway |
| **Parte II, Fase 1** | **Payment, chamada síncrona Order → Payment com retry (backoff exponencial + jitter) e circuit breaker** |

---

## Índice

1. [Arquitetura](#1-arquitetura)
2. [Serviços e endpoints](#2-serviços-e-endpoints)
3. [Resiliência: retry e circuit breaker](#3-resiliência-retry-e-circuit-breaker)
4. [Métricas](#4-métricas)
5. [Como executar](#5-como-executar)
6. [Endereços úteis](#6-endereços-úteis)
7. [Monitorização (Prometheus e Grafana)](#7-monitorização-prometheus-e-grafana)
8. [Testes de carga (k6)](#8-testes-de-carga-k6)
9. [Uso de IA](#9-uso-de-ia)

---

## 1. Arquitetura

- Todo o tráfego dos clientes entra pelo **API Gateway**, que encaminha `/events` para o **Catalog** e `/orders` para o **Order**.
- O **Order** pergunta o preço ao Catalog e cobra a compra ao **Payment** de forma **síncrona** (com retry e circuit breaker). Se o pagamento correr bem, publica `OrderCreated` no **RabbitMQ**.
- O **Notification** consome `OrderCreated` de forma **assíncrona** e simula o envio do email.
- Cada serviço tem acesso exclusivo aos seus dados (Catalog: `events.json`; Payment: SQLite; Notification: `notifications_log.json`).
- O Gateway usa o **Redis** para limitar `POST /orders` a 5 pedidos/s por IP.

| Serviço | Porta (no PC) | Função |
|---|---|---|
| api-gateway | 8000 | Ponto de entrada público; rate limiter em `POST /orders` |
| catalog | 8001 | Metadados dos eventos (concertos, locais, preços) |
| order | 8002 | Cria compras; chama o Payment com retry + circuit breaker; publica `OrderCreated` |
| notification | 8003 | Consome `OrderCreated`, simula o email e expõe métricas |
| payment | 8004 → 8003 | Simula pagamentos, erros e atrasos; SQLite. Dentro do Docker é `payment:8003` |
| rabbitmq | 5672 / 15672 / 15692 | Broker AMQP / consola web / métricas |
| redis | 6379 | Contadores do rate limiter |
| prometheus | 9090 | Recolhe as métricas de todos os serviços |
| grafana | 3000 | Dashboards |
| cadvisor | 8080 | CPU e memória dos containers |
| k6 | – | Testes de carga (perfil `testing`) |

> O Notification e o Payment usam ambos a porta 8003 **dentro** do Docker (contentores diferentes, sem conflito). No PC, o Payment está publicado na **8004** para se poder configurar falhas.

## 2. Serviços e endpoints

### API Gateway (porta 8000)

| Método | Endpoint | Descrição |
|---|---|---|
| GET | `/events` | Pedido síncrono a `catalog:8001/events`; devolve a lista de eventos |
| GET | `/events/{id}` | Pedido síncrono a `catalog:8001/events/{id}`; devolve um evento |
| GET | `/metrics` | Métricas no formato Prometheus |
| POST | `/orders` | Rate limiter (Redis, 5 pedidos/s por IP → **429**) e reencaminha para `order:8002/orders` |
| GET | `/orders/{id}` | Reencaminha para `order:8002/orders/{id}` |

Tratamento de falhas do Catalog:
- Se o Catalog demorar mais de 5 s (timeout), o Gateway devolve **504 Gateway Timeout**.
- Se não conseguir ligar ao Catalog, o Gateway devolve **502 Bad Gateway**.

### Catalog (porta 8001)

| Método | Endpoint | Descrição |
|---|---|---|
| GET | `/events` | Array com todos os eventos disponíveis |
| GET | `/events/{id}` | Detalhes de um evento (404 se não existir) |
| GET | `/metrics` | Métricas no formato Prometheus |

Armazenamento: `catalog/data/events.json`.

Exemplo de resposta de `GET /events/evt-101`:

```json
{
  "id": "evt-101",
  "title": "Coldplay - Music of the Spheres Tour",
  "venue": "Estádio da Luz, Lisbon",
  "date": "2027-05-20T20:00:00Z",
  "price_eur": 85.0,
  "total_tickets": 60000
}
```
### Order (porta 8002)

| Método | Endpoint | Descrição |
|---|---|---|
| POST | `/orders` | Cria e paga uma compra (máx. 4 bilhetes) |
| GET | `/orders/{id}` | Consulta uma compra |
| GET | `/metrics` | Métricas no formato Prometheus |

Corpo do pedido:
```json
{ "costumer_id": "c1", "event_id": "evt-101", "tickets_quantity": 2, "email": "a@b.pt" }
```

Fluxo de `POST /orders`:
1. Pede o preço ao Catalog (`GET catalog:8001/events/{id}`).
2. Cria a compra em `PENDING`.
3. Chama `POST payment:8003/payments` (timeout 2 s), com retry e circuit breaker.
4. Sucesso → `CONFIRMED`, publica `OrderCreated` na fila `orders.created` e devolve **201**.

| Resposta | Quando |
|---|---|
| **201** | Pagamento aceite, compra `CONFIRMED` |
| **404** | Evento não existe no Catalog |
| **422** | Pedido inválido (ex.: mais de 4 bilhetes) |
| **502** | As 3 tentativas ao Payment falharam; compra `FAILED` |
| **503** | Circuito aberto: `{"error": "Payment system unavailable, try again later"}`; compra `FAILED` |
| **503** | Catalog ou RabbitMQ indisponíveis |

### Notification (porta 8003)

| Método | Endpoint | Descrição |
|---|---|---|
| GET | `/metrics` | Métricas no formato Prometheus |

Consome a fila `orders.created` e guarda cada resultado em `notification/data/notifications_log.json`, com `order_id`, `email`, `status` (`SENT` ou `FAILED`) e `processed_at`.

### Payment (porta 8004 no PC, 8003 dentro do Docker)

| Método | Endpoint | Descrição |
|---|---|---|
| POST | `/payments` | `{"order_id", "amount"}` → **201** `SUCCESS` ou **500** `FAILED` (falha simulada) |
| POST | `/payments/configure-faults` | `{"error_rate": 0–1, "latency_seconds": n}`; `{}` repõe o normal |
| GET | `/metrics` | Métricas no formato Prometheus |

Modelo: `id`, `order_id`, `amount`, `status` (`SUCCESS`/`FAILED`), `processed_at`, guardado em SQLite.
Idempotência: o mesmo `order_id` nunca é cobrado duas vezes (um retry depois de um `SUCCESS` devolve o pagamento existente).

Exemplos (PowerShell):
```powershell
# 80% dos pagamentos falham
Invoke-RestMethod -Method Post -Uri http://localhost:8004/payments/configure-faults -ContentType "application/json" -Body '{"error_rate": 0.8}'
# cada pagamento demora 10 s
Invoke-RestMethod -Method Post -Uri http://localhost:8004/payments/configure-faults -ContentType "application/json" -Body '{"latency_seconds": 10}'
# voltar ao normal
Invoke-RestMethod -Method Post -Uri http://localhost:8004/payments/configure-faults -ContentType "application/json" -Body '{}'
```

## 3. Resiliência: retry e circuit breaker

O Order chama o Payment de forma síncrona. Para que uma falha no Payment não arraste o Order, a chamada passa por dois mecanismos.

### Retry com backoff exponencial e jitter
- Até **3 tentativas** por compra, cada uma com timeout de **2 s**.
- Entre tentativas espera `random.uniform(0, 0.1 × 2^tentativa)`: até 0,2 s e depois até 0,4 s.
- O **jitter** (aleatoriedade) evita que muitos clientes repitam o pedido no mesmo instante.
- 3 tentativas é um meio-termo: chega para falhas passageiras, mas limita o tempo de espera (pior caso ≈ 6,6 s) e a carga extra sobre um Payment já com problemas (no máximo 3×).

### Circuit breaker
| Estado | Valor | Comportamento |
|---|---|---|
| CLOSED | 0 | Normal: os pedidos chegam ao Payment |
| OPEN | 1 | Após **5 falhas seguidas**: durante **15 s** não chama o Payment e responde logo **503** |
| HALF-OPEN | 2 | Passados os 15 s deixa passar **um** pedido de teste: sucesso → CLOSED, falha → OPEN |

- Implementado à mão (classe `CircuitBreaker` em `order/main.py`), com um `threading.Lock` porque o FastAPI trata vários pedidos em paralelo.
- Cada tentativa do retry conta para o circuit breaker; com o circuito aberto, o retry pára de imediato.
- Com o circuito aberto, `payment_client_requests_total` **não** aumenta (o Payment nem é chamado) e `circuit_breaker_fallbacks_total` aumenta.
- O estado HALF-OPEN dura apenas um pedido (milissegundos), por isso raramente aparece no Prometheus (scrape de 5 em 5 s).

## 4. Métricas

| Serviço | Métrica | Tipo | Labels |
|---|---|---|---|
| API Gateway | `gateway_requests_total` | Counter | `method`, `endpoint`, `http_status` |
| API Gateway | `gateway_request_duration_seconds` | Histogram | `endpoint` |
| Catalog | `catalog_requests_total` | Counter | `endpoint`, `http_status` |
| Catalog | `catalog_request_duration_seconds` | Histogram | – |
| Notification | `notifications_processed_total` | Counter | `status` (`SENT`, `FAILED`) |
| Notification | `notification_processing_duration_seconds` | Histogram | – |
| API Gateway | `gateway_rate_limited_total` | Counter | – |
| Order | `orders_initiated_total` | Counter | `status` (`CONFIRMED`, `FAILED`) |
| Order | `payment_client_requests_total` | Counter | `status` (`2xx`, `500`, `timeout`, `error`, ...) |
| Order | `circuit_breaker_state` | Gauge | – (0 closed, 1 open, 2 half-open) |
| Order | `circuit_breaker_fallbacks_total` | Counter | – |
| Payment | `payment_requests_total` | Counter | `http_status` |
| Payment | `payment_processing_duration_seconds` | Histogram | – |

As métricas HTTP são registadas num middleware. O label `endpoint` usa o padrão da rota (ex.: `/events/{event_id}`) e não o URL concreto, para não criar uma série nova por cada id. A biblioteca `prometheus_client` também exporta automaticamente métricas do processo (`process_cpu_seconds_total`, `process_resident_memory_bytes`).

No Notification, o contador é incrementado depois de guardar o resultado do envio simulado. O histograma mede o tempo de envio e persistência. Mensagens inválidas descartadas e consultas a `/metrics` não alteram estas métricas. Os valores começam em zero em cada arranque do serviço; o histórico no ficheiro JSON não é recontado.

---

## 5. Como executar

### Requisitos
- Docker Desktop (a correr)
- Git

### Com Docker Compose (recomendado)

Na raiz do projeto (PowerShell / terminal do VS Code):

```powershell
# Construir as imagens e arrancar todos os serviços em segundo plano
docker compose up -d --build

# Ver o estado dos containers
docker compose ps

# Ver os logs de um serviço (Ctrl+C para sair)
docker compose logs -f catalog
docker compose logs -f api-gateway

# Testar os endpoints
curl.exe http://localhost:8000/events
curl.exe http://localhost:8000/events/evt-101
curl.exe http://localhost:8001/metrics
curl.exe http://localhost:8003/metrics
curl.exe http://localhost:8002/metrics   # Order (circuit breaker)

# Teste de resiliência (Payment avaria a meio)
docker compose run --rm k6 run circuit-breaker-test.js

# Recriar o Grafana (depois de alterar provisioning ou dashboards)
docker compose up -d --force-recreate grafana

# Correr o teste de carga k6
docker compose --profile testing run --rm k6 

# teste demo-rate-limiter.js
Get-Content k6\demo-rate-limiter.js | kubectl -n ticketmei run k6-demo --rm -i --restart=Never --image=grafana/k6:0.49.0 -- run -

# Parar e remover os containers
docker compose down
```

### Kubernetes local

Os serviços têm configuração para o cluster Kubernetes local do Docker Desktop.
Consulta [o guia de instalação e publicação no Kubernetes](argocd/README.md).

O Redis está incluído em `k8s/kustomization.yaml`, com uma instância, persistência
AOF num volume de 1 GiB e verificações de arranque, disponibilidade e saúde.
Está acessível dentro do namespace `ticketmei` em `redis:6379`
(`redis.ticketmei.svc.cluster.local:6379` a partir de outros namespaces).
O API Gateway usa o Redis para limitar os pedidos a `POST /orders`.

```sh
kubectl --context docker-desktop apply -k k8s
kubectl --context docker-desktop -n ticketmei rollout status statefulset/redis
kubectl --context docker-desktop -n ticketmei exec redis-0 -- redis-cli -h redis ping
# Resposta esperada: PONG
```

### Argo CD

A instalação e a aplicação GitOps do TicketMEI estão em `argocd/`.
Consulta [o guia de instalação, acesso e sincronização](argocd/README.md).

## 6. Endereços úteis

| URL | Descrição |
|---|---|
| http://localhost:8000/events | Eventos através do Gateway |
| http://localhost:8000/events/evt-101 | Um evento através do Gateway |
| http://localhost:8000/docs | Swagger do Gateway |
| http://localhost:8000/metrics | Métricas do Gateway |
| http://localhost:8001/events | Eventos diretamente no Catalog |
| http://localhost:8001/docs | Swagger do Catalog |
| http://localhost:8001/metrics | Métricas do Catalog |
| http://localhost:8003/metrics | Métricas do Notification |
| http://localhost:9090 | Prometheus |
| http://localhost:9090/targets | Estado dos targets (devem estar todos UP) |
| http://localhost:3000 | Grafana (utilizador `admin`, password `admin`) |
| http://localhost:8080 | cAdvisor |
| http://localhost:8002/docs | Swagger do Order |
| http://localhost:8002/metrics | Métricas do Order (inclui o circuit breaker) |
| http://localhost:8004/docs | Swagger do Payment |
| http://localhost:8004/metrics | Métricas do Payment |
| http://localhost:15672 | Consola do RabbitMQ |

---

## 7. Monitorização (Prometheus e Grafana)

### Prometheus
Configurado em `prometheus/prometheus.yml`, com `scrape_interval: 5s` e os jobs `api-gateway:8000`, `catalog:8001`, `cadvisor:8080`, `order:8002`, `payment:8003` e `notification:8003`.

Queries úteis:

```promql
up                                                                                         # serviços em cima (1)ou em baixo (0)
sum(rate(gateway_requests_total[1m]))                                                      # pedidos por segundo
sum by (http_status) (increase(gateway_requests_total[15m]))                               # respostas por status code
histogram_quantile(0.95, sum by (le) (rate(gateway_request_duration_seconds_bucket[1m])))  # latência p95
sum by (status) (rate(notifications_processed_total[1m]))                                # notificações por segundo e estado
histogram_quantile(0.95, sum by (le) (rate(notification_processing_duration_seconds_bucket[1m]))) # processamento p95
circuit_breaker_state                                          # estado do circuito (0/1/2)
sum by (status) (rate(payment_client_requests_total[1m]))      # tentativas do Order ao Payment
rate(circuit_breaker_fallbacks_total[1m])                      # compras recusadas pelo circuito
sum by (status) (rate(orders_initiated_total[1m]))             # compras confirmadas vs falhadas
sum by (http_status) (rate(payment_requests_total[1m]))        # respostas do Payment
histogram_quantile(0.95, sum by (le) (rate(payment_processing_duration_seconds_bucket[1m])))
```

### Grafana – provisioning
O Grafana configura-se automaticamente no arranque, sem passos manuais:

- **`grafana/provisioning/datasources/datasource.yml`**: cria a datasource Prometheus (`http://prometheus:9090`) com o uid que os dashboards esperam (`a72340b1-d871-4028-bbf1-1795e3553e62`).
- **`grafana/provisioning/dashboards/dashboard.yml`**: carrega todos os dashboards da pasta `/var/lib/grafana/dashboards`.
- No `docker-compose.yml`, o serviço `grafana` monta estes ficheiros por volumes.

### Dashboards
| Dashboard | Painéis |
|---|---|
| **TicketMei - P1** (`dasboard1.json`) | Débito do Gateway (RPS), latência p95/p99, taxa de erros HTTP (%), CPU e memória por container |
| **TicketMEI - Order e Payment** (`k8s/grafana/dashboard-order-payment.json`) | Estado e histórico do circuito, disponibilidade do Payment, erros HTTP, latência p95 e fallbacks |

O dashboard de Order e Payment é carregado pelo Grafana no Compose e no
Kubernetes. Os painéis do circuito precisam de `circuit_breaker_state` e
`circuit_breaker_fallbacks_total` no Order. Os erros e a latência aparecem à
medida que o Prometheus recolhe pedidos de compra e pagamento.

> No Docker Desktop para Windows, o cAdvisor não consegue obter os nomes dos containers, por isso os painéis de CPU/memória por container podem ficar vazios.

---

## 8. Testes de carga (k6)

Script: `k6/load-test.js`. Faz `GET http://api-gateway:8000/events` com:
- subida até **500 utilizadores virtuais** em 3 min, patamar de 2 min e descida em 30 s;
- thresholds: `p(95) < 200 ms` e `http_req_failed < 1%`.

### Resultados (Docker Desktop, um portátil)

| | 1.º teste | 2.º teste |
|---|---|---|
| Pedidos totais | 27 339 | 29 898 |
| Débito | 81,7 req/s | 89,3 req/s |
| Pedidos falhados | 33,0% | 29,7% |
| Latência p95 | 11,6 s | 11,6 s |
| Thresholds | ✗ | ✗ |

### Conclusões
- Com 500 utilizadores na mesma máquina, os serviços **saturam**. Os pedidos ficam em fila, a latência sobe e os que esperam mais de 5 s recebem **504** do Gateway.
- No Prometheus confirmou-se que os erros são **504** (timeouts) e não erros da aplicação.
- A diferença entre os dois testes é variação normal (carga do PC, aquecimento dos serviços).
- No fim do teste, o painel de taxa de erros chega a 100%: nesse momento só restam pedidos em timeout na fila. O valor representativo do teste é cerca de 30%.
- Isto motiva o **rate limiter** da Fase 3: rejeitar o excesso com 429 em vez de deixar a fila crescer.


### Teste de resiliência (Parte II, Fase 1)

Script: `k6/circuit-breaker-test.js`. Fala diretamente com `order:8002` (pelo Gateway, o rate limiter devolveria 429).

| Tempo | Payment | Esperado |
|---|---|---|
| 0–1 min | normal | circuito CLOSED (0) |
| 1–3 min | 80% de erros 500 | circuito abre (1) e testa (2) |
| 3–5 min | normal | circuito volta a CLOSED (0) |

Carga constante de **50 compras/s** (`constant-arrival-rate`).

```powershell
docker compose run --rm k6 run circuit-breaker-test.js
```

#### Resultados (Docker Desktop, um portátil)

| | Valor |
|---|---|
| Compras enviadas | 14 238 (46,5/s; 764 não chegaram a ser enviadas por falta de VUs) |
| 201 CONFIRMED | 7 896 |
| 502 (retry esgotado) | 32 |
| 503 (circuito aberto) | 6 304, todas com a mensagem do enunciado |
| Latência mediana / p95 | 1,5 s / 7,2 s |

#### Conclusões
- O circuito abriu poucos segundos depois de o Payment avariar: só **32** compras esgotaram as 3 tentativas, e as restantes foram recusadas logo com **503**, sem sobrecarregar o Payment.
- Quando o Payment recuperou, o circuito voltou a **CLOSED** sozinho, sem intervenção manual.
- O `http_req_failed` de 44,5% corresponde quase todo aos 503 intencionais.
- A latência elevada mostra que, a 50 compras/s, o **próprio Order é o gargalo**: cada compra faz duas chamadas HTTP e abre uma ligação nova ao RabbitMQ. Melhoria futura: reutilizar a ligação ao RabbitMQ e usar endpoints assíncronos.

---

## 9. Uso de IA

O enunciado permite o uso de ferramentas de IA, desde que identificadas. Ferramentas utilizadas: **Claude** e **Codex**.

| Parte do projeto | Como foi usada |
|---|---|
| Provisioning do Grafana | Diagnóstico do problema "No data" (o dashboard procurava uma datasource com um uid inexistente) e proposta dos ficheiros `datasource.yml`, `dashboard.yml` e dos volumes no `docker-compose.yml`. |
| Análise dos testes k6 | Ajuda a interpretar os resultados (saturação, timeouts 504, variação entre testes). |
| Documentação | Estruturação deste README. |
| Aprendizagem | Explicação de conceitos (Docker, Compose, Prometheus, PromQL, Grafana, k6) e revisão do projeto. |
| Redis, Argo CD e CI/CD | Codex: configuração do Redis no Kubernetes, instalação e configuração do Argo CD, diagnóstico da sincronização e correção da publicação de imagens para AMD64 e ARM64. |
| Payment | Codex: implementação dos endpoints, simulação de falhas e atrasos, persistência SQLite e integração com Compose, Kubernetes, Prometheus e CI. |
| Order: retry e circuit breaker | Claude: explicação dos padrões, proposta passo a passo da classe `CircuitBreaker`, do retry com jitter e da resposta 503; revisão e correção de erros no código escrito pelo grupo. |
| Teste k6 de resiliência | Claude: script `circuit-breaker-test.js` e interpretação dos resultados. |

Todo o código foi executado e testado localmente pelo grupo.
