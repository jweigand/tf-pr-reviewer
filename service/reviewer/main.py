"""Consume candidates, review them, produce results, commit the offset.

One message at a time, deliberately. Reviews are not parallelised because the two
model calls for a single pull request share an identical prompt prefix, and Ollama
only reuses that prefix if the calls arrive back to back with nothing in between.
Concurrency would quietly cost more than it bought. Redpanda holds the backlog.
"""

import json
import logging
import signal
import sys

from confluent_kafka import Consumer, KafkaError, Producer

from . import config, review

log = logging.getLogger("reviewer")

_running = True


def _stop(signum, _frame):
    """Finish the message in flight, then leave the loop and close cleanly."""
    global _running
    log.info("signal %s received, shutting down after the current message", signum)
    _running = False


def produce(producer: Producer, topic: str, key: str, record: dict) -> None:
    """Produce one record and block until the broker has acknowledged it.

    flush() waits for the delivery callback, so by the time this returns without
    raising, the record is on the topic. That ordering is what makes committing the
    consumer offset afterwards safe.
    """
    errors = []

    def on_delivery(err, _msg):
        if err is not None:
            errors.append(err)

    producer.produce(
        topic,
        key=key.encode(),
        value=json.dumps(record).encode(),
        on_delivery=on_delivery,
    )
    producer.flush()
    if errors:
        raise RuntimeError(f"produce to {topic} failed: {errors[0]}")


def handle(producer: Producer, cfg: config.Config, raw: bytes) -> None:
    candidate = json.loads(raw)
    record = review.review(candidate, cfg)
    if record is None:
        # Already reviewed at this head SHA. Producing anything here would overwrite
        # the existing verdict through the sink's upsert. See review.review.
        return
    key = f"{record['repo']}#{record['pr_number']}"
    produce(producer, cfg.reviews_topic, key, record)
    log.info(
        "%s -> %s%s",
        key,
        record["status"],
        f" ({record['reason']})" if record["reason"] else "",
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        stream=sys.stdout,
    )
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    cfg = config.from_env()
    log.info("starting: %s", cfg.describe())

    consumer = Consumer(
        {
            "bootstrap.servers": cfg.brokers,
            "group.id": cfg.consumer_group,
            # Read the backlog that ingest already published, not just new messages.
            "auto.offset.reset": "earliest",
            # Offsets are committed by hand, after the result is safely on the topic.
            # With auto-commit a crash between reviewing and producing would lose the
            # review silently, which is the one failure mode worth engineering against.
            "enable.auto.commit": False,
        }
    )
    producer = Producer({"bootstrap.servers": cfg.brokers})
    consumer.subscribe([cfg.candidates_topic])

    try:
        while _running:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    log.error("consume error: %s", msg.error())
                continue

            try:
                handle(producer, cfg, msg.value())
            except json.JSONDecodeError as exc:
                # Unrecoverable: this message will never parse, so redelivering it
                # forever would wedge the partition. Commit past it and say so loudly.
                log.error(
                    "dropping unparseable message at offset %s: %s", msg.offset(), exc
                )
            except Exception:
                # Anything else is assumed transient (broker down, produce rejected).
                # Do not commit, so the message is redelivered. At-least-once: the
                # Postgres upsert makes a repeated review idempotent.
                log.exception("failed at offset %s, will retry", msg.offset())
                continue

            consumer.commit(msg, asynchronous=False)
    finally:
        log.info("closing consumer")
        consumer.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
