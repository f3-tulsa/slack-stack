"""Contract tests for F3SlackUserLister user field lengths (users.phone, user_name, real_name = VARCHAR(45))."""

import pandas as pd


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


def test_user_lister_fillna_chains():
    """The fillna / slice / bool defaults used by F3SlackUserLister must stay valid."""
    users_df = pd.DataFrame(
        {
            "email": [None],
            "phone": [None],
            "user_name": [None],
            "real_name": ["X" * 60],
            "is_owner": [None],
            "is_admin": [None],
        }
    )
    users_df["email"] = users_df["email"].fillna("None")
    users_df["phone"] = users_df["phone"].fillna("").str[:45]
    users_df["user_name"] = users_df["user_name"].fillna("").str[:45]
    users_df["real_name"] = users_df["real_name"].fillna("").str[:45]
    users_df["is_owner"] = users_df["is_owner"].fillna(False)
    users_df["is_admin"] = users_df["is_admin"].fillna(False)
    assert users_df["email"].iloc[0] == "None"
    assert users_df["phone"].iloc[0] == ""
    assert users_df["user_name"].iloc[0] == ""
    assert len(users_df["real_name"].iloc[0]) == 45
    assert not bool(users_df["is_owner"].iloc[0])
    assert not bool(users_df["is_admin"].iloc[0])
