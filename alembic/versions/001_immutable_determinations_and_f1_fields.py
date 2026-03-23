"""Add F1 completion test fields and immutable determination trigger.

Adds: benefit_type, risk_score, risk_factors, latency_ms, outcome_feedback,
outcome_recorded_at to clinical_determinations.

Creates PostgreSQL trigger to prevent UPDATE/DELETE on clinical_determinations
(append-only enforcement for Constitution F1 completion test 6).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new columns to clinical_determinations
    op.add_column("clinical_determinations", sa.Column("benefit_type", sa.String(50), nullable=True, index=True))
    op.add_column("clinical_determinations", sa.Column("risk_score", sa.Float(), nullable=True))
    op.add_column("clinical_determinations", sa.Column("risk_factors", sa.JSON(), nullable=True))
    op.add_column("clinical_determinations", sa.Column("latency_ms", sa.Float(), nullable=True))
    op.add_column("clinical_determinations", sa.Column("outcome_feedback", sa.String(50), nullable=True))
    op.add_column("clinical_determinations", sa.Column("outcome_recorded_at", sa.DateTime(), nullable=True))

    # Create index on benefit_type
    op.create_index("ix_clinical_determinations_benefit_type", "clinical_determinations", ["benefit_type"])

    # PostgreSQL-only: immutable determination trigger
    # Constitution: "Every determination recorded in an immutable, append-only,
    # cryptographically secured log."
    # This trigger prevents UPDATE or DELETE on clinical_determinations.
    # The ONLY exception is the outcome_feedback column, which can be set once
    # (from NULL to a value) for accuracy measurement.
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute(text("""
            CREATE OR REPLACE FUNCTION prevent_determination_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                -- Allow single-write to outcome_feedback (NULL -> value) for accuracy tracking
                IF TG_OP = 'UPDATE' THEN
                    IF OLD.outcome_feedback IS NULL AND NEW.outcome_feedback IS NOT NULL
                       AND OLD.determination_id = NEW.determination_id
                       AND OLD.claim_id IS NOT DISTINCT FROM NEW.claim_id
                       AND OLD.inputs_encrypted IS NOT DISTINCT FROM NEW.inputs_encrypted
                       AND OLD.decision = NEW.decision
                       AND OLD.reasoning = NEW.reasoning
                       AND OLD.audit_hash = NEW.audit_hash
                    THEN
                        RETURN NEW;
                    END IF;
                END IF;
                RAISE EXCEPTION 'clinical_determinations is append-only. Updates and deletes are prohibited.';
            END;
            $$ LANGUAGE plpgsql;
        """))
        op.execute(text("""
            CREATE TRIGGER enforce_immutable_determinations
            BEFORE UPDATE OR DELETE ON clinical_determinations
            FOR EACH ROW EXECUTE FUNCTION prevent_determination_mutation();
        """))


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute(text("DROP TRIGGER IF EXISTS enforce_immutable_determinations ON clinical_determinations;"))
        op.execute(text("DROP FUNCTION IF EXISTS prevent_determination_mutation();"))

    op.drop_index("ix_clinical_determinations_benefit_type", table_name="clinical_determinations")
    op.drop_column("clinical_determinations", "outcome_recorded_at")
    op.drop_column("clinical_determinations", "outcome_feedback")
    op.drop_column("clinical_determinations", "latency_ms")
    op.drop_column("clinical_determinations", "risk_factors")
    op.drop_column("clinical_determinations", "risk_score")
    op.drop_column("clinical_determinations", "benefit_type")
