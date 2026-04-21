"""merge heads 0060 & 5d5003afe14e

Revision ID: ded9673f946e
Revises: 0060, 5d5003afe14e
Create Date: 2026-04-22 00:42:30.421448

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ded9673f946e'
down_revision: Union[str, None] = ('0060', '5d5003afe14e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
