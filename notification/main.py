import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pika
from prometheus_client import Counter, Histogram, start_http_server


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("notification")

RABBITMQ_URL = os.getenv(
    "RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/ticketmei"
)
QUEUE_NAME = "orders.created"
EMAIL_DELAY_SECONDS = 0.1
LOG_FILE = Path(__file__).parent / "data" / "notifications_log.json"

NOTIFICATIONS_PROCESSED = Counter(
    "notifications_processed_total",
    "Total de notificações processadas por estado",
    ["status"],
)
PROCESSING_DURATION = Histogram(
    "notification_processing_duration_seconds",
    "Tempo de processamento das notificações, incluindo envio e persistência",
)
for status in ("SENT", "FAILED"):
    NOTIFICATIONS_PROCESSED.labels(status=status)


def write_notifications_log(notifications):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = LOG_FILE.with_suffix(".tmp")
    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(notifications, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        # Substituir o ficheiro apenas depois de terminar a escrita do JSON.
        temporary_file.replace(LOG_FILE)
    finally:
        temporary_file.unlink(missing_ok=True)


def save_notification(order_id, email, status):
    notifications = []
    if LOG_FILE.exists():
        with LOG_FILE.open(encoding="utf-8") as file:
            notifications = json.load(file)
        if not isinstance(notifications, list):
            raise ValueError("notifications_log.json deve conter uma lista JSON")

    notifications.append(
        {
            "order_id": order_id,
            "email": email,
            "status": status,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    write_notifications_log(notifications)


def process_notification(channel, method, properties, body):
    # O Order publica {"type": "OrderCreated", "order": {...}}.
    try:
        message = json.loads(body)
        if message["type"] != "OrderCreated":
            raise ValueError("Tipo de mensagem diferente de OrderCreated")

        order = message["order"]
        order_id = order["id"]
        email = order["email"]
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("Identificador da compra em falta")
        if not isinstance(email, str) or not email:
            raise ValueError("Email em falta")
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        logger.warning("Mensagem inválida descartada: %s", exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    started_at = time.perf_counter()
    # Simula o envio do email durante 100 ms, sem enviar um email real.
    status = "SENT"
    try:
        time.sleep(EMAIL_DELAY_SECONDS)
    except Exception:
        status = "FAILED"
        logger.exception("Falha no envio simulado: order_id=%s email=%s", order_id, email)

    save_notification(order_id, email, status)
    NOTIFICATIONS_PROCESSED.labels(status=status).inc()
    PROCESSING_DURATION.observe(time.perf_counter() - started_at)
    logger.info(
        "Notificação processada (email simulado): order_id=%s email=%s status=%s",
        order_id,
        email,
        status,
    )

    channel.basic_ack(delivery_tag=method.delivery_tag)


def main():
    if not LOG_FILE.exists():
        write_notifications_log([])

    start_http_server(8003)
    logger.info("Métricas Prometheus disponíveis na porta 8003 em /metrics")

    while True:
        try:
            parameters = pika.URLParameters(RABBITMQ_URL)
            parameters.socket_timeout = 3
            parameters.blocked_connection_timeout = 3

            with pika.BlockingConnection(parameters) as connection:
                channel = connection.channel()
                channel.queue_declare(queue=QUEUE_NAME, durable=True)
                channel.basic_qos(prefetch_count=1)
                channel.basic_consume(
                    queue=QUEUE_NAME,
                    on_message_callback=process_notification,
                    auto_ack=False,
                )

                logger.info("À espera de mensagens OrderCreated na fila %s", QUEUE_NAME)
                channel.start_consuming()
        except (pika.exceptions.AMQPError, OSError, ValueError) as exc:
            logger.warning(
                "Falha no processamento ou na ligação; nova tentativa em 5 s: %s", exc
            )
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Notification terminado")
