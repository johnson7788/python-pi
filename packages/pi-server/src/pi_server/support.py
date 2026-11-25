"""Small, dependency-free helpers shared by the route modules."""

from __future__ import annotations

from typing import Any


def user_row_to_json(row: Any) -> dict[str, Any]:
    """The admin-facing shape of an account.  Never includes the password hash."""
    return {
        "uid": row["uid"],
        "username": row["username"],
        "displayName": row["display_name"],
        "role": row["role"],
        "status": row["status"],
        "mustChangePassword": bool(row["must_change_password"]),
        "monthlyTokenQuota": row["monthly_token_quota"],
        "monthlyCostQuota": row["monthly_cost_quota"],
        "createdAt": row["created_at"],
        "lastLoginAt": row["last_login_at"],
    }

