"""Add appeals, provider offers, disputes, and provider notifications tables.

Constitution compliance: F1 Q9-Q11 (appeals), F2 Q6 (notifications),
F2 Q8-Q9 (disputes), F2 Q16-Q17 (provider offers).
"""

from alembic import op
import sqlalchemy as sa


revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Appeals (F1 Q9-Q11) ---
    op.create_table(
        "appeals",
        sa.Column("appeal_id", sa.String(36), primary_key=True),
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("claims.claim_id"), index=True),
        sa.Column("employee_id", sa.String(36), sa.ForeignKey("employees.employee_id"), index=True),
        sa.Column("benefit_type", sa.String(50)),
        sa.Column("appeal_type", sa.String(50), default="standard"),
        sa.Column("stage", sa.String(50), default="internal_review"),
        sa.Column("status", sa.String(50), default="pending"),
        sa.Column("requested_at", sa.DateTime()),
        sa.Column("decision_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime()),
        sa.Column("reviewer_type", sa.String(50), nullable=True),
        sa.Column("original_denial_reasoning", sa.Text(), nullable=True),
        sa.Column("appeal_rationale", sa.Text(), nullable=True),
        sa.Column("new_evidence", sa.JSON(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("decision_reasoning", sa.Text(), nullable=True),
        sa.Column("guidelines_referenced", sa.JSON(), nullable=True),
        sa.Column("audit_hash", sa.String(128), nullable=True),
    )

    # --- Provider Offers (F2 Q16-Q17) ---
    op.create_table(
        "provider_offers",
        sa.Column("offer_id", sa.String(36), primary_key=True),
        sa.Column("provider_npi", sa.String(10), index=True),
        sa.Column("service_code", sa.String(20), index=True),
        sa.Column("offered_price", sa.Numeric(12, 2)),
        sa.Column("volume_capacity", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.DateTime()),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(50), default="active"),
        sa.Column("created_at", sa.DateTime()),
    )

    # --- Disputes (F2 Q8-Q9) ---
    op.create_table(
        "disputes",
        sa.Column("dispute_id", sa.String(36), primary_key=True),
        sa.Column("claim_id", sa.String(100), index=True),
        sa.Column("provider_npi", sa.String(10), index=True),
        sa.Column("dispute_type", sa.String(50)),
        sa.Column("status", sa.String(50), default="open"),
        sa.Column("provider_stated_amount", sa.Numeric(12, 2)),
        sa.Column("system_verified_amount", sa.Numeric(12, 2)),
        sa.Column("published_price_reference", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolution_method", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )

    # --- Provider Notifications (F2 Q6) ---
    op.create_table(
        "provider_notifications",
        sa.Column("notification_id", sa.String(36), primary_key=True),
        sa.Column("provider_npi", sa.String(10), index=True),
        sa.Column("claim_id", sa.String(100), index=True),
        sa.Column("notification_type", sa.String(50), default="pre_service_auth"),
        sa.Column("confirmed_amount", sa.Numeric(12, 2)),
        sa.Column("payment_method", sa.String(50)),
        sa.Column("expected_payment_timeline", sa.String(100)),
        sa.Column("status", sa.String(50), default="sent"),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("provider_notifications")
    op.drop_table("disputes")
    op.drop_table("provider_offers")
    op.drop_table("appeals")
