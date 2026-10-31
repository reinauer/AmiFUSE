"""The Amiga clock and the dates on a volume, in the host's local time.

An Amiga's clock keeps local time, counted from 1978-01-01, and a volume
stores each date as a DateStamp in that time: days since 1978-01-01,
minutes into the day and ticks (1/50 s) into the minute. AmiFUSE answers
every clock read a handler makes (dos.library's DateStamp, timer.device's
GetSysTime and TR_GETSYSTIME, intuition.library's CurrentTime) from
amiga_seconds(), and reads a stored DateStamp back with host_time(), so
that a date makes the round trip unchanged.

Each conversion takes the UTC offset in force at the instant converted,
so a summer date and a winter date each get their own.
"""

import calendar
import math
import time
from typing import Tuple

# 1978-01-01 in seconds since the Unix epoch: 2922 days.
AMIGA_EPOCH = 2922 * 24 * 60 * 60
TICKS_PER_SECOND = 50
# ds_Days is a LONG.
MAX_DAYS = 0x7FFFFFFF


def utc_offset(t: float) -> int:
    """Seconds east of UTC of the host's local time at Unix time t.

    Taken from the local date and time rather than tm_gmtoff, which
    Windows gets wrong by 2**32 after 2038. A time the platform cannot
    convert (Windows refuses negative times and years past 3000) takes the
    current offset.
    """
    try:
        return calendar.timegm(time.localtime(t)[:6]) - math.floor(t)
    except (OverflowError, OSError, ValueError):
        now = math.floor(time.time())
        return calendar.timegm(time.localtime(now)[:6]) - now


def amiga_seconds(t: float) -> float:
    """Unix time t as local seconds since 1978-01-01."""
    return t + utc_offset(t) - AMIGA_EPOCH


def sys_time() -> Tuple[int, int]:
    """The system time now, as (seconds, microseconds) since 1978-01-01."""
    secs = amiga_seconds(time.time())
    whole = int(secs)
    return whole, int((secs - whole) * 1_000_000)


def to_datestamp(t: float) -> Tuple[int, int, int]:
    """Unix time t as a local DateStamp (days, minutes, ticks), to the nearest tick.

    A time before 1978-01-01 gives 1978-01-01, the earliest an Amiga can
    store, and one past the last day a LONG can count gives that day's
    last tick.
    """
    ticks = round(max(amiga_seconds(t), 0.0) * TICKS_PER_SECOND)
    minutes, tick = divmod(ticks, 60 * TICKS_PER_SECOND)
    days, minute = divmod(minutes, 24 * 60)
    if days > MAX_DAYS:
        return MAX_DAYS, 24 * 60 - 1, 60 * TICKS_PER_SECOND - 1
    return days, minute, tick


def host_time(days: int, minute: int, tick: int) -> float:
    """Unix time of a local DateStamp."""
    local = AMIGA_EPOCH + days * 86400 + minute * 60 + tick / TICKS_PER_SECOND
    # The offset is the one at the instant itself, which a first guess,
    # off by at most the offset, finds.
    return local - utc_offset(local - utc_offset(local))
