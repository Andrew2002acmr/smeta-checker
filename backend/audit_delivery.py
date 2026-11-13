"""At-least-once delivery with DB leases; duplicate broker messages are harmless."""

from __future__ import annotations

import logging
from uuid import UUID

logger = logging.getLogger(__name__)


def publish_pending(repository, dispatch):
    delivered = 0
    for task in repository.reserve_deliveries():
        try:
            dispatch(
                task["name"],
                {
                    **task["payload"],
                    "delivery_id": task["id"],
                    "delivery_token": task["token"],
                },
            )
            delivered += 1
        except Exception:
            # The delivery lease expires even if this process dies between
            # reserving and publishing. Never discard the committed intent.
            logger.warning("task delivery deferred: %s", task["id"], exc_info=True)
    return delivered


def execute_delivery(context, delivery_id, delivery_token, execute):
    if not delivery_id or not delivery_token:
        # Existing messages from a previous deployment remain supported.
        return execute()
    task_id, token = UUID(str(delivery_id)), UUID(str(delivery_token))
    if not context.repository.claim_delivery(
        task_id, token, timeout=context.settings.task_time_limit
    ):
        return {"duplicate": True}
    try:
        result = execute()
    except Exception as exc:
        context.repository.settle_delivery(
            task_id, token, error=f"{type(exc).__name__}: {str(exc)[:800]}"
        )
        raise
    context.repository.settle_delivery(
        task_id, token, continuation=bool(result.get("continue"))
    )
    return result
