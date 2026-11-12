import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "web" / "ovirt_backup_scheduler.py"
SPEC = importlib.util.spec_from_file_location("ovirt_backup_scheduler", MODULE_PATH)
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)


def test_daily_calendar_expression():
    cfg = scheduler.normalize_config({
        "enabled": False,
        "time": "02:30",
        "weekdays": list(scheduler.DAY_ORDER),
        "timezone": "UTC",
        "randomized_delay_seconds": 0,
    })
    assert scheduler.calendar_expression(cfg) == "*-*-* 02:30:00 UTC"


def test_selected_weekday_calendar_expression():
    cfg = scheduler.normalize_config({
        "enabled": False,
        "time": "03:15",
        "weekdays": ["fri", "mon", "wed", "mon"],
        "timezone": "UTC",
        "randomized_delay_seconds": 60,
    })
    assert cfg["weekdays"] == ["mon", "wed", "fri"]
    assert (
        scheduler.calendar_expression(cfg)
        == "Mon,Wed,Fri *-*-* 03:15:00 UTC"
    )


def test_dropin_resets_packaged_calendar():
    cfg = scheduler.normalize_config({
        "enabled": False,
        "time": "04:00",
        "weekdays": ["sun"],
        "timezone": "UTC",
        "randomized_delay_seconds": 120,
    })
    rendered = scheduler.render_dropin(cfg)
    assert "[Timer]" in rendered
    assert "OnCalendar=\n" in rendered
    assert "OnCalendar=Sun *-*-* 04:00:00 UTC" in rendered
    assert "Persistent=true" in rendered
    assert "RandomizedDelaySec=120" in rendered


@pytest.mark.parametrize("bad_time", ["", "2:00", "24:00", "12:60", "abc"])
def test_invalid_times_rejected(bad_time):
    with pytest.raises(scheduler.SchedulerError):
        scheduler.normalize_config({
            "time": bad_time,
            "timezone": "UTC",
        })


def test_empty_weekday_list_rejected():
    with pytest.raises(scheduler.SchedulerError):
        scheduler.normalize_config({
            "weekdays": [],
            "timezone": "UTC",
        })


def test_unknown_setting_rejected():
    with pytest.raises(scheduler.SchedulerError):
        scheduler.normalize_config({
            "timezone": "UTC",
            "unexpected": True,
        })
