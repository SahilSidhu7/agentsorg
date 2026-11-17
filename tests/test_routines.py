from datetime import datetime

import pytest

from agentsorg import routines as R

from test_org import org  # noqa: F401  (fixture)


def test_schedule_parsing():
    mon_10 = datetime(2026, 10, 26, 10, 0)            # a Monday
    assert R.last_slot("mon 09:00", mon_10) == datetime(2026, 10, 26, 9, 0)
    assert R.last_slot("mon 11:00", mon_10) == datetime(2026, 10, 19, 11, 0)
    assert R.next_slot("weekdays 08:30", datetime(2026, 10, 30, 9, 0)) == datetime(2026, 11, 2, 8, 30)   # Fri -> Mon
    assert R.last_slot("hourly", mon_10.replace(minute=17)) == mon_10
    assert R.next_slot("tue,thu 17:30", mon_10) == datetime(2026, 10, 27, 17, 30)
    for bad in ("every day", "mon 25:00", "fortnightly"):
        with pytest.raises(ValueError):
            R.parse(bad)


def test_ceo_actions_and_goals_in_prompt(org):
    assert org.run_action({"action": "add_routine", "dept": "sales", "every": "mon 09:00",
                           "task": "pipeline review"}).startswith("✓")
    assert org.run_action({"action": "add_routine", "dept": "sales", "every": "sometimes", "task": "x"}).startswith("✗")
    assert org.run_action({"action": "add_goal", "goal": "10 paying customers by Dec"}).startswith("✓")
    facts = org.company_facts()
    assert "GOALS:" in facts and "10 paying customers" in facts and "mon 09:00 sales: pipeline review" in facts
    assert "10 paying customers by Dec" in org.system_prompt("sales", org.c.depts["sales"].members[0].id)
