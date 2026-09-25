from qq_bot.health import mark_failure, mark_success, snapshot
from qq_bot.resilience import FailureBackoff


def test_failure_backoff_grows_and_caps() -> None:
    backoff = FailureBackoff(30, 120)

    assert [backoff.failure() for _ in range(5)] == [30, 60, 120, 120, 120]
    backoff.success()
    assert backoff.failures == 0
    assert backoff._last_log == 0
    assert backoff.failure() == 30


def test_failure_logs_first_error_and_then_throttles() -> None:
    backoff = FailureBackoff(10, 60, log_interval=300)
    backoff.failure()

    assert backoff.should_log(now=1000) is True
    assert backoff.should_log(now=1100) is False
    assert backoff.should_log(now=1300) is True


def test_component_health_recovers_after_success() -> None:
    mark_failure("test-component", "暂时不可用")
    assert snapshot("test-component").state == "error"
    assert snapshot("test-component").failures == 1

    mark_success("test-component", "正常")
    assert snapshot("test-component").state == "ok"
    assert snapshot("test-component").failures == 0
