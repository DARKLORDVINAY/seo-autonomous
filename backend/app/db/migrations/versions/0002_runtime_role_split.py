"""Add a semantically inert site coordination column.

Revision ID: 0002_runtime_role_split
Revises: 0001_canonical
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_runtime_role_split"
down_revision = "0001_canonical"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sites",
        sa.Column("coordination_token", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("sites", "coordination_token")
