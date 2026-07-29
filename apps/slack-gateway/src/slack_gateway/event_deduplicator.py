from collections import OrderedDict
from threading import Lock
from time import monotonic

# This in-memory deduplicator is suitable for a single gateway process.
# If the gateway is scaled to multiple containers or processes, replace it
# with a shared store such as Redis and claim event IDs atomically.
class EventDeduplicator:
    def __init__(
        self,
        ttl_seconds: float = 86_400.0,
        max_entries: int = 10_000,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")

        if max_entries <= 0:
            raise ValueError("max_entries must be greater than zero")

        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._event_ids: OrderedDict[str, float] = OrderedDict()
        self._lock = Lock()

    def claim(self, event_id: str) -> bool:
        now = monotonic()

        with self._lock:
            self._remove_expired_entries(now)

            if event_id in self._event_ids:
                return False

            self._event_ids[event_id] = now

            while len(self._event_ids) > self._max_entries:
                self._event_ids.popitem(last=False)

            return True

    def _remove_expired_entries(self, now: float) -> None:
        expiration_threshold = now - self._ttl_seconds

        while self._event_ids:
            _, received_at = next(iter(self._event_ids.items()))

            if received_at > expiration_threshold:
                break

            self._event_ids.popitem(last=False)
