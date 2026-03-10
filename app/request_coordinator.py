import threading
from typing import Callable


class RequestCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._challenge_streak = 0

    def wait(
        self,
        *,
        min_interval: float,
        max_interval: float,
        sleep_fn: Callable[[float], None],
        jitter_fn: Callable[[float, float], float],
        monotonic_fn: Callable[[], float],
    ) -> None:
        target_interval = max(0.0, float(jitter_fn(min_interval, max_interval)))
        now = monotonic_fn()

        with self._lock:
            scheduled_at = max(now, self._cooldown_until, self._next_request_at)
            if self._next_request_at > 0:
                scheduled_at = max(scheduled_at, self._next_request_at + target_interval)
            self._next_request_at = scheduled_at

        wait_time = scheduled_at - now
        if wait_time > 0:
            sleep_fn(wait_time)

    def note_challenge(
        self,
        *,
        base_cooldown_seconds: float,
        max_cooldown_seconds: float,
        monotonic_fn: Callable[[], float],
    ) -> float:
        with self._lock:
            now = monotonic_fn()
            self._challenge_streak = min(self._challenge_streak + 1, 6)
            cooldown_seconds = min(
                max(base_cooldown_seconds, 0.0) * (2 ** (self._challenge_streak - 1)),
                max(max_cooldown_seconds, 0.0),
            )
            self._cooldown_until = max(self._cooldown_until, now + cooldown_seconds)
            self._next_request_at = max(self._next_request_at, self._cooldown_until)
            return cooldown_seconds

    def note_success(self) -> None:
        with self._lock:
            self._challenge_streak = 0
