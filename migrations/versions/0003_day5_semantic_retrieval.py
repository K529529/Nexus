"""Day 5 repository semantic index, without checkpoint ownership changes."""

from alembic import op

from nexus.infrastructure.semantic_models import SemanticChunkRow, SemanticIndexRow

revision = "day05_0003"
down_revision = "day03_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    SemanticIndexRow.__table__.create(op.get_bind())
    SemanticChunkRow.__table__.create(op.get_bind())


def downgrade() -> None:
    SemanticChunkRow.__table__.drop(op.get_bind())
    SemanticIndexRow.__table__.drop(op.get_bind())
    # The shared extension may have predated Nexus and may serve other schemas.
