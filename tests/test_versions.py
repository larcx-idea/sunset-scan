import pytest

from sunset_scan.versions import base_version, parse_range, parse_version, satisfies


def test_parse_version():
    assert parse_version("7.37.5") == (7, 37, 5)
    assert parse_version("v24.0") == (24, 0, 0)
    assert parse_version("10.0.0-rc.1") == (10, 0, 0)
    assert parse_version("latest") is None


@pytest.mark.parametrize(
    "version, rng, expected",
    [
        ("24.0.1", ">=24.0.0, <25.0.0", True),
        ("25.0.0", ">=24.0.0, <25.0.0", False),
        ("7.37.5", "<=7.37.5", True),
        ("7.38.0", "<=7.37.5", False),
        ("10.15.0", ">=10.0.0", True),
        ("9.39.1", ">=10.0.0", False),
        ("3.9.9", "<4.0.0", True),
        ("4.0.0", "<4.0.0", False),
        ("1.2", "==1.2.0", True),
        (None, ">=1.0.0", True),  # unknown version: treated as affected
        ("1.0.0", None, True),
    ],
)
def test_satisfies(version, rng, expected):
    assert satisfies(version, rng) is expected


def test_base_version():
    assert base_version("^7.33.2") == "7.33.2"
    assert base_version("~9.0") == "9.0"
    assert base_version(">=18 <20") == "18"
    assert base_version("git+https://github.com/x/y") is None
    assert base_version("workspace:*") is None


def test_bad_range():
    with pytest.raises(ValueError):
        parse_range("about 5")
