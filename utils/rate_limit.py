import time
from collections import OrderedDict

# OrderedDict so we can evict oldest entries and keep memory bounded.
_last_request: "OrderedDict[int, float]" = OrderedDict()
RATE_LIMIT_SECONDS = 3
_MAX_TRACKED_USERS = 10_000


def is_rate_limited(user_id: int) -> bool:
    """Return True if the user is rate-limited.

    The timestamp is only updated on a successful (non-limited) call so a user
    spamming requests cannot keep pushing their own window forward and getting
    stuck in a permanent rate-limited state.
    """
    now = time.time()
    last = _last_request.get(user_id)
    if last is not None and now - last < RATE_LIMIT_SECONDS:
        return True

    _last_request[user_id] = now
    _last_request.move_to_end(user_id)
    if len(_last_request) > _MAX_TRACKED_USERS:
        _last_request.popitem(last=False)
    return False
