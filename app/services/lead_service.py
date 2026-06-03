from __future__ import annotations

from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from ..extensions import db
from ..models.lead import Lead
from .validators import is_valid_email, is_valid_phone


class LeadService:
    """Lead capture service used by both API and chat flow."""

    @staticmethod
    def create_lead(payload: dict[str, Any]) -> Lead:
        name = (payload.get("name") or "").strip()
        company = (payload.get("company") or "").strip() or None
        phone = (payload.get("phone") or "").strip() or None
        email = (payload.get("email") or "").strip() or None
        city = (payload.get("city") or "").strip() or None
        project_type = (payload.get("project_type") or "").strip() or None
        nr_parking_spots_raw = payload.get("nr_parking_spots")

        if not name:
            raise ValueError("'name' is required")

        if not phone:
            raise ValueError("'phone' is required")

        if not is_valid_phone(phone):
            raise ValueError("'phone' is invalid")

        if email and not is_valid_email(email):
            raise ValueError("'email' is invalid")

        nr_parking_spots: int | None = None
        if nr_parking_spots_raw not in (None, ""):
            try:
                nr_parking_spots = int(nr_parking_spots_raw)
                if nr_parking_spots <= 0:
                    nr_parking_spots = None
            except (TypeError, ValueError):
                nr_parking_spots = None

        lead = Lead(
            name=name,
            company=company,
            phone=phone,
            email=email,
            nr_parking_spots=nr_parking_spots,
            city=city,
            project_type=project_type,
        )

        try:
            db.session.add(lead)
            db.session.commit()
        except SQLAlchemyError as exc:
            db.session.rollback()
            raise exc

        return lead
