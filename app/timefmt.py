"""Portable time formatting (no %-d, which Windows strftime lacks)."""

from __future__ import annotations

from datetime import date, datetime


def clock(dt: datetime) -> str:
    """'7:30 PM', '11 AM'."""
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {suffix}" if dt.minute else f"{hour} {suffix}"


def day(d: date) -> str:
    """'Sat Nov 14'."""
    return f"{d:%a %b} {d.day}"


def day_long(d: date) -> str:
    """'Saturday, November 14'."""
    return f"{d:%A, %B} {d.day}"


def stamp(dt: datetime) -> str:
    """'Sat Nov 14, 7:30 PM'."""
    return f"{day(dt.date())}, {clock(dt)}"
