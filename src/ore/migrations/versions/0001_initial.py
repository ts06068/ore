"""Initial ORE 0.1 schema; upgrades are explicit and downgrade never deletes a corpus."""
from alembic import op
from ore.store import metadata
revision='0001'
down_revision=None
branch_labels=None
depends_on=None

def upgrade():metadata.create_all(op.get_bind(),checkfirst=True)
def downgrade():raise RuntimeError('Destructive corpus schema downgrade is intentionally unavailable')
