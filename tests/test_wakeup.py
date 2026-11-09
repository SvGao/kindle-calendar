from datetime import datetime, time

from app import wakeup

NS, NE = time(23, 0), time(6, 0)


def dt(h, m, s=0):
    return datetime(2026, 11, 7, h, m, s)


def test_daytime_aligns_to_next_slot_plus_grace():
    assert wakeup.next_slot(dt(14, 3), 15, NS, NE) == dt(14, 15)
    assert wakeup.seconds_until_wakeup(dt(14, 3), 15, NS, NE) == 12 * 60 + 60


def test_exact_slot_moves_to_following_slot():
    assert wakeup.next_slot(dt(14, 15), 15, NS, NE) == dt(14, 30)


def test_last_slot_before_night_jumps_to_morning_when_skipping():
    assert wakeup.next_slot(dt(22, 50), 15, NS, NE, skip_night=True) == datetime(2026, 11, 8, 6, 0)


def test_night_sleeps_until_night_end_when_skipping():
    assert wakeup.next_slot(dt(23, 30), 15, NS, NE, skip_night=True) == datetime(2026, 11, 8, 6, 0)
    assert wakeup.next_slot(dt(2, 0), 15, NS, NE, skip_night=True) == dt(6, 0)


def test_night_keeps_cadence_by_default():
    assert wakeup.next_slot(dt(23, 30), 15, NS, NE) == dt(23, 45)
    assert wakeup.next_slot(dt(2, 0), 15, NS, NE) == dt(2, 15)


def test_full_refresh_window():
    assert wakeup.full_refresh_due(dt(6, 1), NE, 15)
    assert not wakeup.full_refresh_due(dt(6, 15), NE, 15)
    assert not wakeup.full_refresh_due(dt(5, 59), NE, 15)
