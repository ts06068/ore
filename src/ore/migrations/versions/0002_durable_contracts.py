"""Label pre-manifest runs without rewriting their execution history or artifacts."""
from alembic import op
from sqlalchemy import select, update
from ore.store import jobs

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for row in connection.execute(select(jobs.c.id, jobs.c.details)).mappings():
        details = dict(row['details'] or {})
        if 'audit_contract' not in details:
            details['audit_contract'] = 'legacy_bounded'
            connection.execute(update(jobs).where(jobs.c.id == row['id']).values(details=details))


def downgrade():
    raise RuntimeError('Use a database backup to restore the earlier runtime; corpus history is retained')
