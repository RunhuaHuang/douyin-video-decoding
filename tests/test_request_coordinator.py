from app.request_coordinator import RequestCoordinator


def test_request_coordinator_serializes_waits():
    coordinator = RequestCoordinator()
    now = {"value": 100.0}
    sleeps = []

    def monotonic() -> float:
        return now["value"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds

    coordinator.wait(
        min_interval=0.0,
        max_interval=0.0,
        sleep_fn=sleep,
        jitter_fn=lambda a, b: 0.0,
        monotonic_fn=monotonic,
    )
    coordinator.wait(
        min_interval=2.0,
        max_interval=2.0,
        sleep_fn=sleep,
        jitter_fn=lambda a, b: 2.0,
        monotonic_fn=monotonic,
    )

    assert sleeps == [2.0]


def test_request_coordinator_applies_and_clears_challenge_cooldown():
    coordinator = RequestCoordinator()
    now = {"value": 50.0}
    sleeps = []

    def monotonic() -> float:
        return now["value"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds

    cooldown = coordinator.note_challenge(
        base_cooldown_seconds=30.0,
        max_cooldown_seconds=300.0,
        monotonic_fn=monotonic,
    )
    coordinator.wait(
        min_interval=0.0,
        max_interval=0.0,
        sleep_fn=sleep,
        jitter_fn=lambda a, b: 0.0,
        monotonic_fn=monotonic,
    )
    coordinator.note_success()

    assert cooldown == 30.0
    assert sleeps == [30.0]


def test_request_coordinator_tracks_attempt_challenge_signal_until_consumed():
    coordinator = RequestCoordinator()

    coordinator.reset_attempt_signal()
    cooldown = coordinator.note_challenge(
        base_cooldown_seconds=30.0,
        max_cooldown_seconds=300.0,
        monotonic_fn=lambda: 10.0,
        reason="HTTP 403",
    )
    coordinator.note_success()

    assert cooldown == 30.0
    assert coordinator.consume_attempt_signal_reasons() == ("HTTP 403",)
    assert coordinator.consume_attempt_signal() is False
