"""Volume units per provider and per market.

``kline_1d.volume`` is 股 — Sina fills it that way and it is the incumbent. Every
other channel has to be converted to match, and the conversion is **not uniform
across markets**:

    Sina / Tencent ratio, settled bar 2026-10-15, measured against stored history

        688 科创板    n=6 daily, n=144 intraday   ratio 1.00    -> Tencent is already 股
        300 创业板    n=5 daily, n=96  intraday   ratio 100.00  -> Tencent is 手
        60  沪主板    n=4 daily, n=96  intraday   ratio 100.00  -> Tencent is 手
        00  深主板    n=5 daily, n=96  intraday   ratio 100.00  -> Tencent is 手
        92  北交所    (no comparable settled bars in the sample)

So Tencent reports 科创板 in 股 and every other market in 手. A blanket ×100 is
wrong for 688xxx by a factor of 100, and the mismatch is invisible in the response
itself: the bars parse, the closes match to the last decimal, and only the volume
is off. It was found by the sync's own fallback guard, which compares a fallback
against stored history before writing and refused 57 科创板 symbols with
``volume ratio 100.0 is not 1.0``.

East money needed no such rule: its ``f56`` is 手 in every market checked,
including 科创板 (``688079``: 10,653,200 against a stored 10,653,233).
"""

from __future__ import annotations

LOTS_TO_SHARES = 100.0

# Prefixes whose Tencent volume is already in 股. 科创板 is 688xxx, with 689xxx
# used for CDR listings and treated the same way.
TENCENT_SHARE_DENOMINATED_PREFIXES = ("688", "689")


def is_tencent_share_denominated(code: str) -> bool:
    """True when Tencent reports this symbol's volume in 股 rather than 手."""

    text = str(code).strip()
    if text[:2].lower() in {"sh", "sz", "bj"}:
        text = text[2:]
    return text.startswith(TENCENT_SHARE_DENOMINATED_PREFIXES)


def tencent_volume_to_shares(code: str, raw_volume: float) -> float:
    """Convert Tencent's volume field for ``code`` into 股.

    Returns the value unchanged for 科创板, which Tencent already states in shares,
    and multiplies by 100 for every other market.

    A value that is not a number is a caller error and raises. Coercing it to 0.0
    would write ``volume = 0``, which reads as "this bar had no trading" — the
    substitution this project forbids. Both callers parse the field before calling
    (``float(item[5])`` on the daily path, ``safe_int`` on the minute path), so the
    strictness costs nothing and keeps the contract honest.
    """

    if isinstance(raw_volume, bool) or not isinstance(raw_volume, (int, float, str)):
        raise TypeError(f"volume must be a number, got {type(raw_volume).__name__}")
    value = float(raw_volume)
    if is_tencent_share_denominated(code):
        return value
    return value * LOTS_TO_SHARES
