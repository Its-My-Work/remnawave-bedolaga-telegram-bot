"""add mtproxy columns

Revision ID: fe03614db249
Revises: 0053
Create Date: 2026-04-08 00:12:35.565377
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.database.models import AwareDateTime


revision: str = "fe03614db249"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("referral_multiplier", sa.Integer(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "referral_multiplier_expires_at",
            AwareDateTime(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "referral_multiplier_expires_at")
    op.drop_column("users", "referral_multiplier")
