from backend.kite_api.throttle import TokenBucket


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_bucket_spaces_calls_at_rate():
    clk = FakeClock()
    b = TokenBucket(3.0, clock=clk, sleep=clk.sleep)
    times = []
    for _ in range(10):
        b.acquire()
        times.append(clk.t)
    # first call free, then 1/3 s apart => 10 calls take 3 s
    assert abs(times[-1] - 3.0) < 1e-9
    gaps = [b2 - b1 for b1, b2 in zip(times, times[1:])]
    assert all(abs(g - 1 / 3) < 1e-9 for g in gaps)


def test_bucket_refills_after_idle():
    clk = FakeClock()
    b = TokenBucket(2.0, clock=clk, sleep=clk.sleep)
    b.acquire()
    clk.t += 10  # long idle; capacity 1 means no burst beyond one call
    assert b.acquire() == 0.0
    assert b.acquire() > 0.0
