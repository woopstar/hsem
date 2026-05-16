"""Re-export stub — datetime_utils moved to utils/datetime_utils.py.

All public symbols are re-exported from the new location so that existing
imports continue to work without changes.  New code should import directly
from ``custom_components.hsem.utils.datetime_utils``.
"""

from custom_components.hsem.utils.datetime_utils import (  # noqa: F401
    as_tz,
    normalize_datetime,
    normalize_slot_start,
    now,
    slot_key,
    utc_now_iso,
)
