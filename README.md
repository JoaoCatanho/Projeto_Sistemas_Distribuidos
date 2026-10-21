# TicketMEI – Plataforma de venda de bilhetes para eventos

Projeto da unidade curricular de **Sistemas Distribuídos** (MEI, Universidade da Madeira, 2026/2027).

**Fase 1:** serviços **API Gateway** e **Catalog**, orquestrados com Docker Compose e monitorizados com Prometheus e Grafana. A carga é testada com k6.

---

## Índice

1. [Arquitetura](#1-arquitetura)
2. [Serviços e endpoints](#3-serviços-e-endpoints)
3. [Métricas](#4-métricas)
4. [Como executar](#5-como-executar)
5. [Endereços úteis](#6-endereços-úteis)
6. [Monitorização (Prometheus e Grafana)](#7-monitorização-prometheus-e-grafana)
7. [Testes de carga (k6)](#8-testes-de-carga-k6)
8. [Uso de IA](#11-uso-de-ia)

---

## 1. Arquitetura

- Todo o tráfego dos clientes entra pelo **API Gateway**, que encaminha os pedidos para o **Catalog**.
- O Catalog tem acesso exclusivo aos seus dados (`catalog/data/events.json`).
- Todos os componentes correm em containers na mesma rede Docker (`ticketmei-net`) e comunicam pelo nome do serviço (ex.: `http://catalog:8001`).

| Serviço | Porta | Função |
|---|---|---|
| api-gateway | 8000 | Ponto de entrada público; reencaminha os pedidos para o Catalog |
| catalog | 8001 | Serve os metadados dos eventos (concertos, locais, preços) |
| prometheus | 9090 | Recolhe as métricas de todos os serviços |
| grafana | 3000 | Dashboards de monitorização |
| cadvisor | 8080 | Métricas de CPU e memória dos containers |
| k6 | – | Testes de carga (perfil `testing`) |

---

## 2. Serviços e endpoints

### API Gateway (porta 8000)

| Método | Endpoint | Descrição |
|---|---|---|
| GET | `/events` | Pedido síncrono a `catalog:8001/events`; devolve a lista de eventos |
| GET | `/events/{id}` | Pedido síncrono a `catalog:8001/events/{id}`; devolve um evento |
| GET | `/metrics` | Métricas no formato Prometheus |

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

---

## 3. Métricas

| Serviço | Métrica | Tipo | Labels |
|---|---|---|---|
| API Gateway | `gateway_requests_total` | Counter | `method`, `endpoint`, `http_status` |
| API Gateway | `gateway_request_duration_seconds` | Histogram | `endpoint` |
| Catalog | `catalog_requests_total` | Counter | `endpoint`, `http_status` |
| Catalog | `catalog_request_duration_seconds` | Histogram | – |

As métricas são registadas num middleware. O label `endpoint` usa o padrão da rota (ex.: `/events/{event_id}`) e não o URL concreto, para não criar uma série nova por cada id. A biblioteca `prometheus_client` também exporta automaticamente métricas do processo (`process_cpu_seconds_total`, `process_resident_memory_bytes`).

---

## 4. Como executar

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

# Recriar o Grafana (depois de alterar provisioning ou dashboards)
docker compose up -d --force-recreate grafana

# Correr o teste de carga k6
docker compose --profile testing run --rm k6

# Parar e remover os containers
docker compose down
```

## 5. Endereços úteis

| URL | Descrição |
|---|---|
| http://localhost:8000/events | Eventos através do Gateway |
| http://localhost:8000/events/evt-101 | Um evento através do Gateway |
| http://localhost:8000/docs | Swagger do Gateway |
| http://localhost:8000/metrics | Métricas do Gateway |
| http://localhost:8001/events | Eventos diretamente no Catalog |
| http://localhost:8001/docs | Swagger do Catalog |
| http://localhost:8001/metrics | Métricas do Catalog |
| http://localhost:9090 | Prometheus |
| http://localhost:9090/targets | Estado dos targets (devem estar todos UP) |
| http://localhost:3000 | Grafana (utilizador `admin`, password `admin`) |
| http://localhost:8080 | cAdvisor |

---

## 6. Monitorização (Prometheus e Grafana)

### Prometheus
Configurado em `prometheus/prometheus.yml`, com `scrape_interval: 5s` e três jobs: `api-gateway:8000`, `catalog:8001` e `cadvisor:8080`.

Queries úteis:

```promql
up                                                                                         # serviços em cima (1)ou em baixo (0)
sum(rate(gateway_requests_total[1m]))                                                      # pedidos por segundo
sum by (http_status) (increase(gateway_requests_total[15m]))                               # respostas por status code
histogram_quantile(0.95, sum by (le) (rate(gateway_request_duration_seconds_bucket[1m])))  # latência p95
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

> No Docker Desktop para Windows, o cAdvisor não consegue obter os nomes dos containers, por isso os painéis de CPU/memória por container podem ficar vazios.

---

## 7. Testes de carga (k6)

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

---

## 8. Uso de IA

O enunciado permite o uso de ferramentas de IA, desde que identificadas. Ferramenta utilizada: **Claude**.

| Parte do projeto | Como foi usada |
|---|---|
| Provisioning do Grafana | Diagnóstico do problema "No data" (o dashboard procurava uma datasource com um uid inexistente) e proposta dos ficheiros `datasource.yml`, `dashboard.yml` e dos volumes no `docker-compose.yml`. |
| Análise dos testes k6 | Ajuda a interpretar os resultados (saturação, timeouts 504, variação entre testes). |
| Documentação | Estruturação deste README. |
| Aprendizagem | Explicação de conceitos (Docker, Compose, Prometheus, PromQL, Grafana, k6) e revisão do projeto. |

Todo o código foi executado e testado localmente pelo grupo.