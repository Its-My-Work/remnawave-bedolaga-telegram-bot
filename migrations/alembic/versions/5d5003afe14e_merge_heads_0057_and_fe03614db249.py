"""merge heads 0057 and fe03614db249

Revision ID: 5d5003afe14e
Revises: 0057, fe03614db249
Create Date: 2026-04-13 23:19:34.555995

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5d5003afe14e'
down_revision: Union[str, None] = ('0057', 'fe03614db249')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
