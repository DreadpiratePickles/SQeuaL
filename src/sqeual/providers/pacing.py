"""Spread a burst of model calls out under a per-minute quota.

One question is `k` generation calls plus one explain call plus two judge calls,
issued back to back. A free-tier quota is usually per minute, and bounded retries
cannot ride out a window that long — so the burst is paced rather than half of it
coming back as rate-limit errors that are honestly recorded and useless.

The sleeping is project 1's `pacing.pace`, imported rather than rewritten: a
second opinion in this repository about how to space calls under somebody else's
quota is a second thing to get wrong. This is only the small piece of state that
`pace` needs and does not keep — when the previous call started.
"""

import time

from regression_detect.pacing import pace, validate_interval


class Pacer:
    """Remembers when the last call started, so the next one can wait."""

    def __init__(self, min_interval_ms: int = 0) -> None:
        """Args:
            min_interval_ms: the minimum gap between call starts. Zero disables
                pacing, which is the right default offline and the wrong one
                against a free tier.

        Raises:
            ValueError: `min_interval_ms` is not a non-negative integer.
        """
        self.min_interval_ms = validate_interval(min_interval_ms)
        self._previous_start: float | None = None
        self.waits = 0
        """How many calls actually had to sleep. Recorded in the trace, because
        a run that spent two minutes waiting and a run that spent two minutes
        thinking look identical in a wall-clock total."""

    def wait(self) -> None:
        """Sleep if the next call would start too soon after the last one.

        Whether a wait was due is decided **before** `pace` is called, not by
        measuring how long it took. Timing it looks equivalent and is not: the
        monotonic clock advances across any call at all, so a measured
        implementation counts a wait on every request even at
        `min_interval_ms = 0`, and the trace then reports pacing that never
        happened.
        """
        if self._previous_start is not None and self.min_interval_ms > 0:
            due = self._previous_start + self.min_interval_ms / 1000
            if time.monotonic() < due:
                self.waits += 1
        self._previous_start = pace(self._previous_start, self.min_interval_ms)
