import math
from datetime import datetime, timezone

EARLIEST_TIMESTAMP = datetime(1, 1, 2, tzinfo=timezone.utc).timestamp()
LATEST_TIMESTAMP = datetime(9999, 12, 30, tzinfo=timezone.utc).timestamp()


def showable_timestamp(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not EARLIEST_TIMESTAMP <= value <= LATEST_TIMESTAMP:
        return None
    return float(value)


def timestamp_of(moment: datetime) -> float | None:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return showable_timestamp(moment.timestamp())
