"""Constitution gap closure: appeals, provider portal, security models.

Revision ID: 002
Revises: 001
Create Date: 2026-03-25

Adds tables for:
- appeals (F1 Q9-11: ERISA/ACA-compliant appeals process)
- appeal_timeline (F1 Q9-11: append-only appeal event log)
- provider_authorizations (F2 Q6-9: pre-service notification and charge validation)
- provider_disputes (F2 Q6-9: dispute filing and resolution with price trail)
- baa_records (F13 Q2: BAA lifecycle management)
- breach_incidents (F13 Q5-6: breach detection and response)
- security_assessments (F13 Q11: third-party security assessment)

Modifies:
- providers: adds availability_schedule JSON column (F9 Q3)
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Appeals (F1 Q9-11)
    op.create_table(
        "appeals",
        sa.Column("appeal_id", sa.String(36), primary_key=True),
        sa.Column("determination_id", sa.String(36), sa.ForeignKey("clinical_determinations.determination_id"), nullable=True),
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("claims.claim_id"), nullable=True),
        sa.Column("appeal_type", sa.String(50), nullable=False),
        sa.Column("appeal_status", sa.String(50), nullable=False, server_default="filed"),
        sa.Column("appeal_reason", sa.Text, nullable=False),
        sa.Column("reviewer_type", sa.String(50), nullable=True),
        sa.Column("reviewer_id", sa.String(255), nullable=True),
        sa.Column("reviewer_notes", sa.Text, nullable=True),
        sa.Column("filed_at", sa.DateTime, nullable=False),
        sa.Column("review_deadline", sa.DateTime, nullable=False),
        sa.Column("reviewed_at", sa.DateTime, nullable=True),
        sa.Column("outcome", sa.String(50), nullable=True),
        sa.Column("denial_notice_text", sa.Text, nullable=True),
        sa.Column("appeal_rights_explanation", sa.Text, nullable=True),
        sa.Column("iro_organization", sa.String(255), nullable=True),
        sa.Column("iro_assigned_at", sa.DateTime, nullable=True),
        sa.Column("is_expedited", sa.Boolean, server_default="0"),
        sa.Column("expedited_reason", sa.Text, nullable=True),
        sa.Column("audit_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_appeals_claim_id", "appeals", ["claim_id"])

    op.create_table(
        "appeal_timeline",
        sa.Column("timeline_id", sa.String(36), primary_key=True),
        sa.Column("appeal_id", sa.String(36), sa.ForeignKey("appeals.appeal_id"), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("occurred_at", sa.DateTime, nullable=False),
        sa.Column("details", sa.JSON, nullable=True),
        sa.Column("actor", sa.String(255), nullable=False),
    )
    op.create_index("ix_appeal_timeline_appeal_id", "appeal_timeline", ["appeal_id"])

    # Provider Portal (F2 Q6-9)
    op.create_table(
        "provider_authorizations",
        sa.Column("auth_id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(36), sa.ForeignKey("providers.provider_id"), nullable=False),
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("claims.claim_id"), nullable=True),
        sa.Column("service_code", sa.String(20), nullable=False),
        sa.Column("service_description", sa.String(500), nullable=True),
        sa.Column("benefit_type", sa.String(50), nullable=False),
        sa.Column("confirmed_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("payment_method", sa.String(50), nullable=False),
        sa.Column("estimated_payment_timeline", sa.String(100), nullable=False),
        sa.Column("pre_service_notified_at", sa.DateTime, nullable=True),
        sa.Column("notification_channel", sa.String(50), nullable=True),
        sa.Column("charge_submitted_at", sa.DateTime, nullable=True),
        sa.Column("charge_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("charge_validated", sa.Boolean, nullable=True),
        sa.Column("discrepancy_flag", sa.Boolean, server_default="0"),
        sa.Column("discrepancy_details", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_provider_auth_provider_id", "provider_authorizations", ["provider_id"])
    op.create_index("ix_provider_auth_claim_id", "provider_authorizations", ["claim_id"])

    op.create_table(
        "provider_disputes",
        sa.Column("dispute_id", sa.String(36), primary_key=True),
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("claims.claim_id"), nullable=False),
        sa.Column("provider_id", sa.String(36), sa.ForeignKey("providers.provider_id"), nullable=False),
        sa.Column("authorization_id", sa.String(36), sa.ForeignKey("provider_authorizations.auth_id"), nullable=True),
        sa.Column("dispute_reason", sa.Text, nullable=False),
        sa.Column("disputed_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("published_price_reference", sa.JSON, nullable=True),
        sa.Column("pre_service_confirmation_reference", sa.JSON, nullable=True),
        sa.Column("service_rendered_reference", sa.JSON, nullable=True),
        sa.Column("resolution_method", sa.String(50), nullable=True),
        sa.Column("resolution", sa.Text, nullable=True),
        sa.Column("resolution_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("provider_accepted", sa.Boolean, nullable=True),
        sa.Column("filed_at", sa.DateTime, nullable=False),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="filed"),
        sa.Column("feeding_f8", sa.Boolean, server_default="1"),
    )
    op.create_index("ix_provider_disputes_claim_id", "provider_disputes", ["claim_id"])
    op.create_index("ix_provider_disputes_provider_id", "provider_disputes", ["provider_id"])

    # Security (F13)
    op.create_table(
        "baa_records",
        sa.Column("baa_id", sa.String(36), primary_key=True),
        sa.Column("vendor_name", sa.String(255), nullable=False),
        sa.Column("vendor_type", sa.String(50), nullable=False),
        sa.Column("phi_categories_accessed", sa.JSON, nullable=True),
        sa.Column("signed_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("renewal_status", sa.String(50), nullable=False, server_default="active"),
        sa.Column("contacts", sa.Text, nullable=True),
        sa.Column("baa_document_ref", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "breach_incidents",
        sa.Column("incident_id", sa.String(36), primary_key=True),
        sa.Column("detected_at", sa.DateTime, nullable=False),
        sa.Column("incident_type", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(50), nullable=False),
        sa.Column("affected_records_count", sa.Integer, server_default="0"),
        sa.Column("affected_employers", sa.JSON, nullable=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("detection_method", sa.String(100), nullable=False),
        sa.Column("hhs_notification_required", sa.Boolean, server_default="0"),
        sa.Column("hhs_notified_at", sa.DateTime, nullable=True),
        sa.Column("state_ag_notified_at", sa.DateTime, nullable=True),
        sa.Column("individuals_notified_at", sa.DateTime, nullable=True),
        sa.Column("notification_deadline", sa.DateTime, nullable=True),
        sa.Column("remediation_steps", sa.JSON, nullable=True),
        sa.Column("root_cause", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="detected"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "security_assessments",
        sa.Column("assessment_id", sa.String(36), primary_key=True),
        sa.Column("entity_name", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("assessment_type", sa.String(50), nullable=False),
        sa.Column("overall_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("findings", sa.JSON, nullable=True),
        sa.Column("recommendations", sa.JSON, nullable=True),
        sa.Column("assessed_at", sa.DateTime, nullable=False),
        sa.Column("next_assessment_due", sa.DateTime, nullable=True),
        sa.Column("assessor", sa.String(255), nullable=False),
    )

    # Provider availability (F9 Q3)
    op.add_column("providers", sa.Column("availability_schedule", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("providers", "availability_schedule")
    op.drop_table("security_assessments")
    op.drop_table("breach_incidents")
    op.drop_table("baa_records")
    op.drop_table("provider_disputes")
    op.drop_table("provider_authorizations")
    op.drop_table("appeal_timeline")
    op.drop_table("appeals")
