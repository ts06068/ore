from alembic import context
from sqlalchemy import create_engine
from ore.store import metadata

def run():
    config=context.config
    engine=create_engine(config.attributes['database_url'])
    with engine.connect() as connection:
        context.configure(connection=connection,target_metadata=metadata)
        with context.begin_transaction():context.run_migrations()
    engine.dispose()
run()
