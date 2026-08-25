"""
what it means for a stored submission to have outlived its usefulness

This module holds the retention rule and nothing else: no queries, no HTTP, no
process. It exists so that the one decision worth arguing about is written down
in one place rather than inferred from a WHERE clause.

The rule
--------

A submission may be deleted when it is older than the cutoff **and** the service
will never need its content again. The second half is not a matter of taste. A
queued webhook delivery does not carry a copy of the submitted fields: the worker
loads the submission and builds the payload from it at the moment it sends. So
the payload is needed for as long as any further attempt is possible, which is:

* ``pending``, waiting for its due time;
* ``processing``, claimed by a worker right now;
* ``failed``, because a failed delivery is replayable by an operator and a replay
  puts it back into exactly the state above.

That leaves two cases where nothing will ever read the fields again: a submission
with no delivery at all, because its endpoint has no webhook, and a submission
whose delivery has been ``delivered``. Those, and only those, are eligible.

Delivery history is not part of the bargain. Removing a submission unlinks the
delivery and its attempts from it and leaves both rows exactly where they are:
what this service tried to do, when, and how it went is operational history worth
more than the form content it was carrying. That is what the ``ON DELETE SET
NULL`` in revision ``0005`` buys, and why the delivery carries its own endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from hymical_forms.webhooks import DeliveryState

# The delivery states that keep a submission alive however old it is. Written as
# the states that protect rather than the states that release, so a state added
# later is protective until somebody deliberately decides otherwise.
PROTECTED_DELIVERY_STATES = (
    DeliveryState.PENDING,
    DeliveryState.PROCESSING,
    DeliveryState.FAILED,
)

# How many submissions one delete statement takes. Small enough that a sweep of a
# large backlog is many short transactions rather than one long one holding locks
# across the whole table, and large enough that the round trips do not dominate.
DEFAULT_BATCH_SIZE = 500

# A ceiling on how many batches one run will do, so an operator who starts a
# sweep against an enormous backlog gets it back rather than watching it run
# unbounded. What is left over is removed by running the command again.
MAX_BATCHES = 10_000


class RetentionDisabled(Exception):
    """
    raised when a cutoff was asked for and no retention age is configured
    """

    def __init__(self) -> None:
        """
        say that nothing is eligible because no age has been set
        """
        super().__init__("no submission retention age is configured")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """
    how long submissions are kept before they become eligible for deletion
    """

    # Zero means keep indefinitely, which is the default and the only safe thing
    # for an unset value to mean: a service that started deleting form data
    # because nobody configured it would be indefensible.
    days: int

    @property
    def enabled(self) -> bool:
        """
        report whether this policy makes anything eligible
        :returns: True if a positive retention age is configured
        """
        return self.days > 0

    def cutoff(self, now: datetime) -> datetime:
        """
        work out the instant a submission has to predate to be eligible
        :param now: the instant the sweep is being run at
        :returns: the cutoff in UTC, exclusive, so a submission exactly on it is kept
        :raises RetentionDisabled: if no retention age is configured
        """
        if not self.enabled:
            raise RetentionDisabled()
        return now - timedelta(days=self.days)
