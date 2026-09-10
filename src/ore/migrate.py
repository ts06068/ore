from pathlib import Path
from alembic.config import Config
from alembic import command

def upgrade(database_url):
    config=Config();config.set_main_option('script_location',str(Path(__file__).parent/'migrations'))
    config.attributes['database_url']=database_url
    command.upgrade(config,'head')
