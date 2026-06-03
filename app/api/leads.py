from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..models.lead import Lead
from ..services.lead_service import LeadService


bp = Blueprint("leads", __name__)


@bp.get("/leads")
def list_leads():
	leads = Lead.query.order_by(Lead.created_at.desc()).all()
	return jsonify(
		[
			{
				"id": lead.id,
				"name": lead.name,
				"company": lead.company,
				"phone": lead.phone,
				"email": lead.email,
				"nr_parking_spots": lead.nr_parking_spots,
				"city": lead.city,
				"project_type": lead.project_type,
				"created_at": lead.created_at.isoformat() + "Z",
			}
			for lead in leads
		]
	)


@bp.post("/leads")

def create_lead():
	payload = request.get_json(silent=True) or {}

	try:
		lead = LeadService.create_lead(payload)
	except ValueError as exc:
		return (
			jsonify({"error": "invalid_request", "message": str(exc)}),
			400,
		)

	return (
		jsonify(
			{
				"id": lead.id,
				"created_at": lead.created_at.isoformat() + "Z",
			}
		),
		201,
	)

