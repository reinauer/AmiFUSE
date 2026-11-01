"""Local time for the Amiga clock and the dates on a volume (#146, #160).

The host's UTC offset is patched, or the host put in US Pacific time for
the tests of utc_offset() itself, so the tests do not depend on the
machine's time zone.
"""

import calendar
import errno
import math
import os
import struct
import time

import pytest

from amifuse import amiga_time
from amifuse.amiga_time import AMIGA_EPOCH, MAX_DAYS, host_time, to_datestamp

# 2026-07-01 12:00:00 UTC, and 2026-01-15 12:00:00 UTC.
SUMMER = calendar.timegm((2026, 7, 1, 12, 0, 0))
WINTER = calendar.timegm((2026, 1, 15, 12, 0, 0))
PDT = -7 * 3600
PST = -8 * 3600
# US Pacific time's clocks go forward at 10:00 UTC on 2026-03-08 and back
# at 09:00 UTC on 2026-11-01.
SPRING_CHANGE = calendar.timegm((2026, 3, 8, 10, 0, 0))
AUTUMN_CHANGE = calendar.timegm((2026, 11, 1, 9, 0, 0))
PACIFIC_TZ = "PST8PDT,M3.2.0,M11.1.0"


def _days(year, month, day):
    return (calendar.timegm((year, month, day, 0, 0, 0)) - AMIGA_EPOCH) // 86400


def _pacific_offset(t):
    """US Pacific time's UTC offset at Unix time t: seven hours west from
    02:00 on the second Sunday in March to 02:00 on the first Sunday in
    November, local time, eight otherwise."""
    year = time.gmtime(t).tm_year

    def sunday(month, nth):
        return (6 - calendar.weekday(year, month, 1)) % 7 + 1 + 7 * (nth - 1)

    start = calendar.timegm((year, 3, sunday(3, 2), 2, 0, 0)) - PST
    end = calendar.timegm((year, 11, sunday(11, 1), 2, 0, 0)) - PDT
    return PDT if start <= t < end else PST


@pytest.fixture
def california(monkeypatch):
    """US Pacific time, which changes at 02:00 local time, not at midnight
    UTC, and mid-month in March."""
    monkeypatch.setattr(amiga_time, "utc_offset", _pacific_offset)


@pytest.fixture
def pacific_host(monkeypatch):
    """The host's local time is US Pacific time.

    Where time.tzset() exists, TZ is set for the test. Windows has no
    tzset(), so there localtime() follows the same rule instead.
    """
    if not hasattr(time, "tzset"):
        def localtime(t=None, **kwargs):
            t = time.time() if t is None else t
            return time.gmtime(math.floor(t) + _pacific_offset(t))
        monkeypatch.setattr(time, "localtime", localtime)
        yield
        return
    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = PACIFIC_TZ
        time.tzset()
        yield
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_datestamp_is_local_time_at_each_dates_own_offset(california):
    """Noon UTC is 05:00 in summer and 04:00 in winter, in ticks past the minute."""
    assert to_datestamp(SUMMER + 12.5) == (_days(2026, 7, 1), 5 * 60, 12 * 50 + 25)
    assert to_datestamp(WINTER) == (_days(2026, 1, 15), 4 * 60, 0)


def test_host_time_inverts_datestamp(california):
    """A date reads back as the instant it was written, to the tick."""
    for t in (SUMMER + 12.5, WINTER, WINTER + 59.98):
        assert host_time(*to_datestamp(t)) == pytest.approx(t, abs=0.001)


def test_host_time_takes_the_offset_at_the_instant_itself(california):
    """Just after each change, the local time read as UTC falls before it.

    The offset at that reading is the old one, so it takes a second lookup,
    at the first guess, to find the instant.
    """
    for t in (SPRING_CHANGE + 1800, AUTUMN_CHANGE + 5400):
        assert host_time(*to_datestamp(t)) == pytest.approx(t, abs=0.001)


def test_datestamp_survives_the_round_trip(california):
    """Every tick of a minute, in summer and winter, comes back as it was stored."""
    for days in (_days(2026, 1, 15), _days(2026, 7, 1)):
        for minute in (0, 777, 1439):
            for tick in range(3000):
                assert to_datestamp(host_time(days, minute, tick)) == (days, minute, tick)


def test_date_outside_what_an_amiga_stores_is_clamped(monkeypatch):
    monkeypatch.setattr(amiga_time, "utc_offset", lambda t: 3600)
    assert to_datestamp(0) == (0, 0, 0)
    assert to_datestamp(AMIGA_EPOCH - 3600) == (0, 0, 0)
    assert to_datestamp(1e15) == (MAX_DAYS, 1439, 2999)


def test_utc_offset_is_the_hosts_offset_at_each_instant(pacific_host):
    assert amiga_time.utc_offset(SUMMER + 0.75) == PDT
    assert amiga_time.utc_offset(WINTER) == PST
    assert amiga_time.utc_offset(SPRING_CHANGE - 1) == PST
    assert amiga_time.utc_offset(SPRING_CHANGE) == PDT
    assert amiga_time.utc_offset(AUTUMN_CHANGE - 1) == PDT
    assert amiga_time.utc_offset(AUTUMN_CHANGE) == PST


def test_utc_offset_after_2038_is_an_offset():
    """Windows' tm_gmtoff is off by 2**32 after 2038.

    This one runs on the host's own zone, so on Windows it calls the real
    localtime(), where that error shows.
    """
    t = calendar.timegm((2040, 7, 1, 0, 0, 0))
    assert abs(amiga_time.utc_offset(t)) <= 14 * 3600


def test_utc_offset_after_2038_is_the_hosts_offset(pacific_host):
    assert amiga_time.utc_offset(calendar.timegm((2040, 7, 1, 0, 0, 0))) == PDT
    assert amiga_time.utc_offset(calendar.timegm((2040, 12, 1, 0, 0, 0))) == PST


def test_system_time_is_local_seconds_since_1978(monkeypatch):
    monkeypatch.setattr(amiga_time, "utc_offset", lambda t: PDT)
    before = time.time()
    secs, micros = amiga_time.sys_time()
    after = time.time()
    assert int(before) + PDT - AMIGA_EPOCH <= secs <= after + PDT - AMIGA_EPOCH
    assert 0 <= micros < 1_000_000


class _Mem:
    def __init__(self, data):
        self.data = data

    def r32(self, addr):
        return struct.unpack_from(">I", self.data, addr)[0]

    def r32s(self, addr):
        return struct.unpack_from(">i", self.data, addr)[0]

    def r_block(self, addr, size):
        return self.data[addr:addr + size]


def test_parse_fib_reads_fib_date_as_host_time(california, fuse_mock):
    """fib_Date, at 132, is the entry's date in local time."""
    from amifuse.fuse_fs import _parse_fib

    fib = bytearray(260)
    struct.pack_into(">i", fib, 4, -3)
    fib[8:13] = b"\x04name"
    struct.pack_into(">3i", fib, 132, *to_datestamp(SUMMER + 1.5))
    info = _parse_fib(_Mem(bytes(fib)), 0)
    assert info["mtime"] == pytest.approx(SUMMER + 1.5, abs=0.001)


class _DateVolume:
    """A bridge with one closed file, /f, dated 1e9, whose ACTION_SET_DATE gets reply."""

    def __init__(self, reply):
        self._write_enabled = True
        self.state = None
        self.reply = reply
        self.sent = []

    def stat_path(self, path):
        return {"dir_type": -3, "size": 0, "name": "f", "protection": 0,
                "num_blocks": 0, "mtime": 1e9}

    def locate_path(self, path):
        return 0, 0, []

    def set_date(self, lock, name, when):
        self.sent.append((lock, name, when))
        return self.reply


@pytest.mark.parametrize("reply, error", [
    ((-1, 0), None),
    ((0, 209), None),  # ERROR_ACTION_NOT_KNOWN: the handler keeps no dates
    ((0, 214), errno.EROFS),
    ((0, 202), errno.EBUSY),
    ((0, -1), errno.EIO),  # no reply
])
def test_utimens_sends_set_date_and_reports_a_refusal(fuse_mock, reply, error):
    from amifuse.fuse_fs import AmigaFuseFS, FuseOSError

    volume = _DateVolume(reply)
    ops = AmigaFuseFS(volume, debug=False, icons=False)
    if error is None:
        assert ops.utimens("/f", (1.0, 2e9)) == 0
    else:
        with pytest.raises(FuseOSError) as exc_info:
            ops.utimens("/f", (1.0, 2e9))
        assert exc_info.value.args[0] == error
    assert volume.sent == [(0, "f", 2e9)]


def test_utimens_leaving_the_date_sends_nothing(fuse_mock):
    """WinFsp passes an access-time-only change with the current date."""
    from amifuse.fuse_fs import AmigaFuseFS

    volume = _DateVolume((-1, 0))
    ops = AmigaFuseFS(volume, debug=False, icons=False)
    assert ops.utimens("/f", (2e9, 1e9)) == 0
    assert ops.utimens("/f", (2e9, 1e9 + 1)) == 0
    assert volume.sent == [(0, "f", 1e9 + 1)]
