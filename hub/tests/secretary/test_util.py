"""Small display rules shared by the pages."""

from __future__ import annotations


def test_time_range_label_names_a_later_end_day():
    from app.secretary import util

    t = util.local_ts
    assert util.time_range_label(t("2026-10-04", "09:55"), t("2026-10-04", "10:20")) == "09:55–10:20"
    assert util.time_range_label(t("2026-10-04", "22:10"), t("2026-10-05", "01:40")) == "22:10–01:40"  # one night
    assert util.time_range_label(t("2026-10-02", "20:47"), t("2026-10-04", "12:09")) == "20:47–Sun 12:09"
    assert util.time_range_label(t("2026-10-04", "09:00"), t("2026-10-05", "10:00")) == "09:00–Mon 10:00"
    assert util.time_range_label(t("2026-10-02", "20:47"), t("2026-10-04", "12:09"), running=True) == "20:47–now"
