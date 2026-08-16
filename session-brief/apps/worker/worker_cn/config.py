"""Runtime configuration for the Chinese-side worker, read from the environment."""

import os

from dotenv import load_dotenv

load_dotenv()

# Wall-clock send time for the open brief (Asia/Shanghai). 09:10 is 20 min before
# the 09:30 bell (docs/02 stages the morning as ingest 08:00 → assemble 08:10 →
# send 08:15, but in Shanghai time).
CN_OPEN_SEND_HOUR: int = int(os.environ.get("CN_OPEN_SEND_HOUR", "9"))
CN_OPEN_SEND_MINUTE: int = int(os.environ.get("CN_OPEN_SEND_MINUTE", "10"))

# Minutes after the session close to send the close brief. Shanghai close is 15:00,
# so close + 20 min sends at 15:20.
CN_SEND_DELAY_MINUTES: int = int(os.environ.get("CN_SEND_DELAY_MINUTES", "20"))

# How long to poll the Chinese data provider for today's EOD bar before giving up,
# and how often. Mirrors the US BAR_POLL_TIMEOUT_S / BAR_POLL_INTERVAL_S pattern.
CN_BAR_POLL_TIMEOUT_S: int = int(os.environ.get("CN_BAR_POLL_TIMEOUT_S", "1200"))
CN_BAR_POLL_INTERVAL_S: int = int(os.environ.get("CN_BAR_POLL_INTERVAL_S", "90"))

# Dead-man's switch URLs for the Chinese open and close briefs (docs/02).
# Empty ⇒ no ping.
HEALTHCHECKS_CN_OPEN_URL: str = os.environ.get("HEALTHCHECKS_CN_OPEN_URL", "")
HEALTHCHECKS_CN_CLOSE_URL: str = os.environ.get("HEALTHCHECKS_CN_CLOSE_URL", "")

# Whether Chinese market bars are live (True) or synthetic (False). Env-read
# boolean: "1" and "true" (case-insensitive) are truthy, all others falsy.
_cn_bars_live_str = os.environ.get("CN_BARS_LIVE", "").lower()
CN_BARS_LIVE: bool = _cn_bars_live_str in ("1", "true")


def cn_bars_are_synthetic() -> bool:
    """Whether the Chinese bars are running on invented levels.

    Derived from the CN_BARS_LIVE flag, never hand-flipped — this flag is flipped
    by hand only after `tiingo-cn-probe` passes (cn/docs/open-questions.md CN-Q1..Q5),
    a documented exception to D29's "derive, never hand-flip", because `TIINGO_API_KEY`
    presence already means "US book live" and cannot also encode the CN answer.
    """
    return not CN_BARS_LIVE
