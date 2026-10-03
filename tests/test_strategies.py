"""窗口边界判定测试：固定窗口与滑动窗口的边界行为必须明确可预期。"""

from traffic_guard.strategies import fixed_window_hit, sliding_window_hit


class TestFixedWindow:
    def test_first_request_allowed(self):
        _, d = fixed_window_hit(None, 100.0, limit=3, window_seconds=60.0)
        assert d.allowed and d.remaining == 2 and d.retry_after == 0.0

    def test_window_aligned_to_natural_time(self):
        # 窗口按 floor(now/window)*window 对齐：now=119.9 属于 [60, 120)
        s, d = fixed_window_hit(None, 119.9, limit=1, window_seconds=60.0)
        assert d.allowed
        s, d = fixed_window_hit(s, 119.99, limit=1, window_seconds=60.0)
        assert not d.allowed
        assert abs(d.retry_after - 0.01) < 1e-9

    def test_boundary_instant_belongs_to_next_window(self):
        # now 恰好等于窗口右边界时计入下一个窗口
        s, d = fixed_window_hit(None, 59.999, limit=1, window_seconds=60.0)
        assert d.allowed
        s, d = fixed_window_hit(s, 60.0, limit=1, window_seconds=60.0)
        assert d.allowed and d.remaining == 0

    def test_counter_resets_in_new_window(self):
        s, _ = fixed_window_hit(None, 10.0, limit=1, window_seconds=60.0)
        s, d = fixed_window_hit(s, 10.5, limit=1, window_seconds=60.0)
        assert not d.allowed
        s, d = fixed_window_hit(s, 61.0, limit=1, window_seconds=60.0)
        assert d.allowed

    def test_retry_after_points_to_window_reset(self):
        s, _ = fixed_window_hit(None, 30.0, limit=1, window_seconds=60.0)
        _, d = fixed_window_hit(s, 45.0, limit=1, window_seconds=60.0)
        assert abs(d.retry_after - 15.0) < 1e-9  # 60 - 45


class TestSlidingWindow:
    def test_requests_within_window_counted(self):
        s, d = sliding_window_hit(None, 100.0, limit=2, window_seconds=10.0)
        assert d.allowed
        s, d = sliding_window_hit(s, 105.0, limit=2, window_seconds=10.0)
        assert d.allowed and d.remaining == 0
        s, d = sliding_window_hit(s, 106.0, limit=2, window_seconds=10.0)
        assert not d.allowed

    def test_boundary_record_expires_strictly(self):
        # 统计区间为 (now - window, now]：恰好等于 now - window 的记录被剔除
        s, _ = sliding_window_hit(None, 10.0, limit=1, window_seconds=10.0)
        s, d = sliding_window_hit(s, 20.0, limit=1, window_seconds=10.0)
        assert d.allowed  # 10.0 不在 (10, 20] 内
        # 而 10.001 时刻的记录在 now=20.0 时仍计入
        s2, _ = sliding_window_hit(None, 10.001, limit=1, window_seconds=10.0)
        s2, d2 = sliding_window_hit(s2, 20.0, limit=1, window_seconds=10.0)
        assert not d2.allowed

    def test_denied_request_does_not_consume_quota(self):
        s, _ = sliding_window_hit(None, 10.0, limit=1, window_seconds=10.0)
        s, d = sliding_window_hit(s, 11.0, limit=1, window_seconds=10.0)
        assert not d.allowed
        # 被拒绝的请求不记录，10.0 的记录过期后即放行
        s, d = sliding_window_hit(s, 20.001, limit=1, window_seconds=10.0)
        assert d.allowed

    def test_retry_after_equals_oldest_record_expiry(self):
        s, _ = sliding_window_hit(None, 10.0, limit=1, window_seconds=10.0)
        _, d = sliding_window_hit(s, 14.0, limit=1, window_seconds=10.0)
        assert abs(d.retry_after - 6.0) < 1e-9  # 10 + 10 - 14
