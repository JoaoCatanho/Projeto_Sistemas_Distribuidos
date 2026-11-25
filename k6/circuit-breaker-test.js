// Teste de carga da Parte II, Fase 1: retry + circuit breaker
// Correr (na raiz do projeto) com:
//   docker compose run --rm k6 run circuit-breaker-test.js
//
// O que acontece:
//   0:00 - 1:00  Payment normal                -> circuito CLOSED (0)
//   1:00 - 3:00  Payment avariado (80% de 500)  -> circuito abre (1), testa (2), volta a abrir...
//   3:00 - 5:00  Payment recuperado             -> circuito volta a CLOSED (0)
// Durante os 5 minutos chegam sempre 50 compras por segundo.
//
// Fala diretamente com o Order (order:8002), NAO com o gateway,
// porque o rate limiter do gateway (5 pedidos/s por IP) cortaria quase tudo com 429.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

const ORDER_URL = __ENV.ORDER_URL || 'http://order:8002/orders';
const FAULTS_URL = __ENV.FAULTS_URL || 'http://payment:8003/payments/configure-faults';

// Contadores próprios, para ver no resumo final quantas respostas de cada tipo houve
const confirmadas = new Counter('compras_201_confirmadas');
const pagamentoFalhou = new Counter('compras_502_pagamento_falhou');
const circuitoAberto = new Counter('compras_503_circuito_aberto');

export const options = {
  scenarios: {
    // Cenário 1: 50 compras por segundo, sempre, durante 5 minutos
    compras: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: '5m',
      preAllocatedVUs: 50,
      maxVUs: 300, // se o Order ficar lento, o k6 precisa de mais VUs para manter os 50/s
      exec: 'comprar',
    },
    // Cenário 2: um único "utilizador" que avaria o Payment ao minuto 1 e o repara ao minuto 3
    avaria: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 1,
      startTime: '1m',
      maxDuration: '3m',
      exec: 'avariarPayment',
    },
  },
};

const headers = { headers: { 'Content-Type': 'application/json' } };

export function comprar() {
  const body = JSON.stringify({
    costumer_id: `cliente-${__VU}`,
    event_id: 'evt-101',
    tickets_quantity: 1,
    email: 'teste@ticketmei.pt',
  });
    const res = http.post(ORDER_URL, body, {
        headers: { 'Content-Type': 'application/json' },
        timeout: '30s',
    });

  if (res.status === 201) confirmadas.add(1);
  else if (res.status === 502) pagamentoFalhou.add(1);
  else if (res.status === 503) circuitoAberto.add(1);

  check(res, {
    'resposta esperada (201, 502 ou 503)': (r) => [201, 502, 503].includes(r.status),
    '503 traz a mensagem do enunciado': (r) =>
      r.status !== 503 || (r.json('error') === 'Payment system unavailable, try again later'),
  });
}

export function avariarPayment() {
  // Para usar o atraso de 10 s em vez dos erros, troca por:
  //   JSON.stringify({ latency_seconds: 10 })
  console.log('>>> Minuto 1: a avariar o Payment (80% de erros 500) durante 2 minutos');
  http.post(FAULTS_URL, JSON.stringify({ error_rate: 0.8 }), headers);

  sleep(120);

  console.log('>>> Minuto 3: a repor o Payment ao normal');
  http.post(FAULTS_URL, JSON.stringify({}), headers);
}
