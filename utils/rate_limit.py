import time
from collections import defaultdict

_last_request: dict[int, float] = defaultdict(float)
RATE_LIMIT_SECONDS = 3


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    if now - _last_request[user_id] < RATE_LIMIT_SECONDS:
        return True
    _last_request[user_id] = now
    return False
