"""타임존 — 시각 비교는 전부 거래소 현지시각으로 한다.

UTC 문자열을 그대로 자르면 서머타임 전환 시 같은 09:31 이 다른 시각의 봉과 짝지어진다.
"""

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("scalper")

_TZ = {}
try:
    from zoneinfo import ZoneInfo
    _TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}
except Exception:
    _TZ = {}
    log.warning("zoneinfo 사용 불가 — 고정 오프셋으로 대체한다. "
                "정확한 처리를 위해 `pip install tzdata` 를 권장한다.")


def _us_offset(dt_utc: datetime) -> timedelta:
    """zoneinfo 가 없을 때의 미국 동부 오프셋 근사. 3월 둘째 일요일~11월 첫째 일요일 EDT."""
    y = dt_utc.year
    march = datetime(y, 3, 1, tzinfo=timezone.utc)
    second_sun_mar = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    nov = datetime(y, 11, 1, tzinfo=timezone.utc)
    first_sun_nov = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return timedelta(hours=-4) if second_sun_mar <= dt_utc < first_sun_nov else timedelta(hours=-5)


def to_local(dt: datetime, market: str) -> datetime:
    """UTC-aware datetime 을 거래소 현지시각으로."""
    if market in _TZ:
        return dt.astimezone(_TZ[market])
    off = timedelta(hours=9) if market == "KR" else _us_offset(dt)
    return dt.astimezone(timezone(off))


def now_local(market: str) -> datetime:
    return to_local(datetime.now(timezone.utc), market)


_naive_warned = False


def parse_ts(ts: str, market: str):
    """API 타임스탬프 -> 거래소 현지시각 datetime. 실패 시 None.

    타임존 정보가 없는 문자열이 오면 UTC 로 간주한다. 이 가정이 틀리면
    프로파일 시각이 통째로 어긋나므로, 기동 시 원본 타임스탬프를 로그로 찍어
    사람이 확인하도록 한다.
    """
    global _naive_warned
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        if not _naive_warned:
            log.warning("타임스탬프에 타임존 정보가 없다 — UTC 로 간주한다: %s", ts)
            _naive_warned = True
        dt = dt.replace(tzinfo=timezone.utc)
    return to_local(dt, market)
