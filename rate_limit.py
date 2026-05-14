"""In-memory rate limiter на юзера (sliding window)."""

import time
from collections import defaultdict
from config import RATE_LIMIT_PER_MINUTE

# user_id -> список timestamp'ов запросов
_requests: dict[int, list[float]] = defaultdict(list)


def check_rate_limit(user_id: int) -> bool:
    """Проверяет, не превышен ли лимит запросов.

    Returns:
        True если запрос разрешён, False если лимит превышен.
    """
    now = time.time()
    window = 60.0

    # Убираем старые запросы за пределами окна
    _requests[user_id] = [t for t in _requests[user_id] if now - t < window]

    if len(_requests[user_id]) >= RATE_LIMIT_PER_MINUTE:
        return False

    _requests[user_id].append(now)
    return True


def seconds_until_reset(user_id: int) -> int:
    """Сколько секунд ждать до освобождения слота."""
    if not _requests[user_id]:
        return 0
    oldest = min(_requests[user_id])
    wait = 60.0 - (time.time() - oldest)
    return max(1, int(wait))
