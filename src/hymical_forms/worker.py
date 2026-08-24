"""
the delivery worker: claims owed webhook deliveries and sends them

Run it as its own process, separately from the API::

    python -m hymical_forms.worker

It is deliberately not a FastAPI background task. The point of the outbox is
that the obligation survives the API process, and work that only runs inside
that process would give none of that back.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from datetime import datetime
from types import FrameType

import httpx2
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms import storage, webhooks
from hymical_forms.config import Settings
from hymical_forms.db import create_engine_from_url, create_session_factory
from hymical_forms.delivery import create_webhook_client, deliver
from hymical_forms.models import utcnow

logger = logging.getLogger(__name__)


async def process_batch(
    session: Session,
    client: httpx2.AsyncClient,
    settings: Settings,
    *,
    now: datetime,
) -> int:
    """
    claim whatever is due, deliver it, and record what happened
    :param session: the session to claim and record through
    :param client: the outbound client deliveries are sent with
    :param settings: active configuration, read for the lease, batch size and retry policy
    :param now: the instant to judge dueness against
    :returns: how many deliveries were attempted
    """
    claimed = storage.claim_due_deliveries(
        session,
        now=now,
        lease_seconds=settings.worker_lease_seconds,
        limit=settings.worker_batch_size,
    )
    if not claimed:
        return 0

    submissions = storage.load_submissions(session, [job.submission_id for job in claimed])

    # The network calls overlap so that one unresponsive destination does not
    # hold up the rest of the batch for its whole timeout. They are made with no
    # database transaction open: holding one across somebody else's server would
    # pin a connection for as long as they take to answer.
    bodies = {
        job.id: webhooks.serialize_payload(webhooks.build_payload(submissions[job.submission_id]))
        for job in claimed
    }
    results = await asyncio.gather(
        *(
            deliver(client, url=job.destination_url, secret=job.signing_secret, body=bodies[job.id])
            for job in claimed
        )
    )

    # One instant governs the whole batch: the claim, the attempt records and any
    # backoff are all measured from ``now``. Re-reading the clock per delivery
    # would gain nothing real and would make every retry schedule approximate.
    policy = settings.retry_policy()
    for job, result in zip(claimed, results, strict=True):
        storage.complete_attempt(session, job, result, now=now, policy=policy)
        logger.info(
            "delivery %s attempt %d %s (%s)",
            job.id,
            job.attempts,
            result.outcome,
            job.state,
        )

    return len(claimed)


async def run_worker(settings: Settings, *, stop: threading.Event) -> None:
    """
    poll for due deliveries until asked to stop
    :param settings: active configuration
    :param stop: event that ends the loop once set
    """
    engine = create_engine_from_url(settings.database_url)
    session_factory: sessionmaker[Session] = create_session_factory(engine)
    client = create_webhook_client(settings)

    logger.info("worker started, polling every %.1fs", settings.worker_poll_seconds)
    try:
        while not stop.is_set():
            try:
                with session_factory() as session:
                    handled = await process_batch(session, client, settings, now=utcnow())
            except Exception:
                # A worker that dies on one bad tick stops delivering everything.
                # Whatever was claimed keeps its lease and becomes due again once
                # that lease expires, so the safe move is to log and keep polling.
                logger.exception("delivery batch failed")
                handled = 0

            if handled == 0:
                # Nothing was due, so wait before asking again rather than
                # spinning against the database. A stop request cuts this short.
                await asyncio.to_thread(stop.wait, settings.worker_poll_seconds)
    finally:
        await client.aclose()
        engine.dispose()
        logger.info("worker stopped")


def main() -> None:
    """
    run the worker until the process is asked to shut down
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    settings = Settings()
    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        """
        ask the loop to finish the tick it is on and exit
        :param signum: the signal received
        :param frame: the interrupted stack frame, unused
        """
        logger.info("shutdown requested")
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    asyncio.run(run_worker(settings, stop=stop))


if __name__ == "__main__":
    main()
