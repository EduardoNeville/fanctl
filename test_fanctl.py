#!/usr/bin/env python3
"""Self-check for the fan control policy. Run: python3 test_fanctl.py"""
from fanctl import curve, slew, parse_curve

C = [(40, 20), (60, 40), (80, 100)]


def test_curve_clamps_below_and_above():
    assert curve(C, 20) == 20
    assert curve(C, 90) == 100


def test_curve_interpolates():
    assert curve(C, 50) == 30
    assert curve(C, 70) == 70


def test_curve_handles_unsorted_input():
    assert parse_curve("80:100,40:20") == [(40, 20), (80, 100)]


def test_slew_limits_both_directions():
    assert slew(20, 40, 5) == 25
    assert slew(40, 20, 5) == 35


def test_slew_does_not_overshoot():
    assert slew(38, 40, 5) == 40
    assert slew(42, 40, 5) == 40


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
