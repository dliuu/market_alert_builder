"""Chinese-side trading calendar (XSHG). SSE and SZSE share trading hours and
holidays, so one calendar (Shanghai Stock Exchange, XSHG) covers both (cn/docs
design doc). Unlike NYSE, XSHG has no half-day mechanism — every session
closes at the standard 15:00 CST bell."""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

from worker.calendar import MarketCalendar

CST = ZoneInfo("Asia/Shanghai")
CN = MarketCalendar("XSHG", CST, time(15, 0))
