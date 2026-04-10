"""Add orchestrator routing fields to claims and care_episodes.

Layer 2 → Layer 3 routing persistence and emergent care flags.

Adds to claims:
  - date_of_service: eligibility is checked as of service date, not submission
  - care_episode_id: links a claim back to the CareEpisode that routed it
  - routing_expected_price: price at routing time, compared to billed for drift
  - is_emergent: prudent layperson emergent flag
  - emergent_signals: JSON record of which signals triggered emergent

Adds to care_episodes:
  - routing_decision: full JSON record of Layer 2 provider selection
  - is_preventive: True when episode was created by the proactive scheduler
  - preventive_rule_id: identifier of the USPSTF/ACIP rule that triggered it
"""

from alembic import op
import sqlalchemy as sa


revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- claims table ----
    op.add_column("claims", sa.Column("date_of_service", sa.DateTime(), nullable=True))
    op.add_column(
        "claims",
        sa.Column("care_episode_id", sa.CHAR(32), nullable=True),
    )
    op.create_index(
        "ix_claims_care_episode_id", "claims", ["care_episode_id"]
    )
    op.create_foreign_key(
        "fk_claims_care_episode_id",
        "claims",
        "care_episodes",
        ["care_episode_id"],
        ["episode_id"],
    )
    op.add_column(
        "claims",
        sa.Column("routing_expected_price", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column("claims", sa.Column("is_emergent", sa.Boolean(), nullable=True))
    op.add_column("claims", sa.Column("emergent_signals", sa.JSON(), nullable=True))

    # ---- care_episodes table ----
    op.add_column(
        "care_episodes", sa.Column("routing_decision", sa.JSON(), nullable=True)
    )
    op.add_column(
        "care_episodes",
        sa.Column(
            "is_preventive", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "care_episodes", sa.Column("preventive_rule_id", sa.String(100), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("care_episodes", "preventive_rule_id")
    op.drop_column("care_episodes", "is_preventive")
    op.drop_column("care_episodes", "routing_decision")

    op.drop_column("claims", "emergent_signals")
    op.drop_column("claims", "is_emergent")
    op.drop_column("claims", "routing_expected_price")
    op.drop_constraint("fk_claims_care_episode_id", "claims", type_="foreignkey")
    op.drop_index("ix_claims_care_episode_id", table_name="claims")
    op.drop_column("claims", "care_episode_id")
    op.drop_column("claims", "date_of_service")
