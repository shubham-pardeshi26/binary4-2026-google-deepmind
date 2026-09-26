"""Per-run event bus: ordered history, durable JSONL log, and live fan-out to SSE subscribers.

Every event that the pipeline produces flows through exactly one :class:`EventBus` (one per run):

* it is stamped with ``type``, ``run_id``, ``t`` (ms since the run's ``created_at``) and a monotonically
  increasing ``seq`` (lets SSE handlers splice "history then live" without gaps or duplicates, and lets the
  frontend dedupe after an EventSource reconnect);
* it is appended to the in-memory ``history`` list (replayed to every new subscriber);
* it is appended to ``data/runs/<id>/events.jsonl`` so finished runs can be replayed after a restart;
* it is pushed onto every subscriber's bounded :class:`asyncio.Queue`. A subscriber that falls too far behind
  is *dropped* (its queue is closed with a sentinel) instead of blocking the pipeline or growing memory without
  bound; the browser's EventSource simply reconnects and receives the full history again.

``emit`` is synchronous and never raises (disk errors are logged, not propagated), so it is safe to call from
any coroutine or done-callback on the event loop thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("adloop.events")

#: Maximum number of undelivered events buffered per subscriber before it is considered stuck and dropped.
SUBSCRIBER_QUEUE_SIZE = 2000

#: Sentinel placed on a subscriber queue to tell the consumer the stream is over (dropped or bus closed).
CLOSED = None


class Subscription:
    """A single live consumer of a bus (typically one SSE connection)."""

    __slots__ = ("queue", "dropped")

    def __init__(self, maxsize: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = False

    def close(self) -> None:
        """Mark the subscription finished and wake the consumer with the CLOSED sentinel.

        If the queue is full we discard its backlog first: the consumer is going to reconnect and receive the
        full history anyway, so the backlog has no value.
        """
        self.dropped = True
        while True:
            try:
                self.queue.put_nowait(CLOSED)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - defensive
                    return


class EventBus:
    """Ordered, persisted, fan-out event stream for one run."""

    def __init__(self, run_id: str, run_dir: Path, created_at: float,
                 history: list[dict] | None = None) -> None:
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self.created_at = float(created_at)
        self.history: list[dict] = list(history or [])
        self._seq = max((int(e.get("seq", 0)) for e in self.history), default=0)
        self._subs: set[Subscription] = set()
        self._path = self.run_dir / "events.jsonl"

    # ------------------------------------------------------------------ timing
    def now_ms(self) -> int:
        """Milliseconds elapsed since the run started (the ``t`` stamp of a new event)."""
        return max(0, int((time.time() - self.created_at) * 1000))

    # ------------------------------------------------------------------ publish
    def emit(self, type_: str, **payload: Any) -> dict:
        """Stamp, record, persist and fan out one event. Returns the event dict. Never raises."""
        self._seq += 1
        event = {"type": type_, "run_id": self.run_id, "t": self.now_ms(), "seq": self._seq}
        event.update(payload)
        self.history.append(event)
        self._append_to_disk(event)
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("run %s: dropping slow SSE subscriber (queue full)", self.run_id)
                self._subs.discard(sub)
                sub.close()
        return event

    def _append_to_disk(self, event: dict) -> None:
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception:  # disk trouble must never break the pipeline
            log.exception("run %s: failed to append event to %s", self.run_id, self._path)

    # ------------------------------------------------------------------ subscribe
    def subscribe(self) -> tuple[Subscription, list[dict]]:
        """Register a live subscriber and return it together with a snapshot of the history so far.

        Subscribing and snapshotting happen atomically (no await in between), so the consumer can send the
        snapshot and then the queue contents with no gap; events with ``seq`` <= the snapshot's last ``seq``
        can never appear on the queue.
        """
        sub = Subscription()
        self._subs.add(sub)
        return sub, list(self.history)

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def close(self) -> None:
        """Close every live subscription (used on shutdown)."""
        for sub in list(self._subs):
            sub.close()
        self._subs.clear()

    # ------------------------------------------------------------------ load
    @staticmethod
    def load_history(run_dir: Path) -> list[dict]:
        """Read ``events.jsonl`` from disk, skipping any torn/corrupt trailing line."""
        path = Path(run_dir) / "events.jsonl"
        events: list[dict] = []
        if not path.exists():
            return events
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        events.append(obj)
        except OSError:
            log.exception("failed to read %s", path)
        return events


def sse_format(event: dict | None = None, *, comment: str | None = None) -> str:
    """Format one Server-Sent-Events frame.

    ``sse_format(event)`` -> ``"data: <json>\\n\\n"``; ``sse_format(comment="ping")`` -> ``": ping\\n\\n"``
    (comment frames are ignored by EventSource and serve as keep-alive heartbeats). No ``id:`` field is sent on
    purpose: the contract is "full history on every (re)connect", and the frontend dedupes by ``seq``/scene+idx.
    """
    if comment is not None:
        return f": {comment}\n\n"
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
