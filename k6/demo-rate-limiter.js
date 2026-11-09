// Demo: envia 20 compras por segundo durante 10 segundos ao POST /orders.
// O rate limiter (5 pedidos/s por IP) deve aceitar cerca de 5 por segundo (202)
// e rejeitar o resto com 429 Too Many Requests.
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://api-gateway:8000';
const accepted = new Counter('orders_accepted_202');
const limited = new Counter('orders_rate_limited_429');

export const options = {
  scenarios: {
    burst: {
      executor: 'constant-arrival-rate',
      rate: 20,
      timeUnit: '1s',
      duration: '10s',
      preAllocatedVUs: 20,
    },
  },
  thresholds: {
    orders_rate_limited_429: ['count>0'],
  },
};

export default function () {
  const body = JSON.stringify({
    costumer_id: 'k6-burst',
    event_id: 'evt-101',
    tickets_quantity: 1,
    email: 'burst@teste.pt',
  });
  const res = http.post(`${BASE_URL}/orders`, body, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (res.status === 202) accepted.add(1);
  if (res.status === 429) limited.add(1);
  check(res, { 'resposta e 202 ou 429': (r) => r.status === 202 || r.status === 429 });
}
