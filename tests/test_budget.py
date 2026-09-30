from jev_remote.budget import RequestBudget


def test_step_cap_per_command():
    budget = RequestBudget(max_steps_per_command=3, max_jev_per_hour=0, max_openai_per_hour=0)
    assert budget.exceeded(2) is None
    assert "Step limit" in budget.exceeded(3)


def test_hourly_caps_and_zero_disables():
    budget = RequestBudget(max_steps_per_command=0, max_jev_per_hour=2, max_openai_per_hour=1)
    budget.record(jev_calls=2)
    assert "Jev" in budget.exceeded()
    budget = RequestBudget(max_steps_per_command=0, max_jev_per_hour=0, max_openai_per_hour=1)
    budget.record(jev_calls=500, openai_calls=1)
    assert "OpenAI" in budget.exceeded()
    budget = RequestBudget(0, 0, 0)
    budget.record(1000, 1000)
    assert budget.exceeded(1000) is None


def test_old_calls_expire(monkeypatch):
    budget = RequestBudget(max_steps_per_command=0, max_jev_per_hour=1, max_openai_per_hour=0)
    budget.record(jev_calls=1)
    budget._jev[0] -= RequestBudget.WINDOW + 1
    assert budget.exceeded() is None
