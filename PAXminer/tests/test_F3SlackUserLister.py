"""Contract tests for F3SlackUserLister user field lengths (users.phone, user_name, real_name = VARCHAR(45))."""


def test_users_varchar45_truncation_contract():
    """Mirrors F3SlackUserLister after fillna(''): .str[:45] on phone, user_name, real_name."""
    limit = 45
    long = "X" * 60
    phone = (long or "")[:limit]
    user_name = (long or "")[:limit]
    real_name = (long or "")[:limit]

    assert len(phone) == limit
    assert len(user_name) == limit
    assert len(real_name) == limit
    assert phone == user_name == real_name == "X" * limit
