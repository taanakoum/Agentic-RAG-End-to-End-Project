"""Ownership checks. Other users' resources are indistinguishable from missing ones (404)."""

from __future__ import annotations

import uuid

from app.core.exceptions import ResourceNotFoundError


def require_owner(owner_id: uuid.UUID | None, current_user_id: uuid.UUID) -> None:
    """Raise ResourceNotFoundError unless the resource exists and belongs to the current user.

    404 rather than 403 so the API never reveals that someone else's resource exists.
    `owner_id` is None when the lookup found nothing.
    """
    if owner_id is None or owner_id != current_user_id:
        raise ResourceNotFoundError()
