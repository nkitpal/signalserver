"""Alembic environment: the URL comes from the app settings unless set explicitly."""

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import create_engine, pool

from signalserver_gui.config import get_settings
from signalserver_gui.models import Base

config = context.config
target_metadata = Base.metadata


def _include(obj, name, type_, reflected, compare_to):
    # Ignore PostGIS / tiger schema tables that live in the same database.
    if type_ == "table" and reflected and compare_to is None:
        return False
    return alembic_helpers.include_object(obj, name, type_, reflected, compare_to)


def run_migrations() -> None:
    url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          include_object=_include, compare_type=True,
                          process_revision_directives=alembic_helpers.writer,
                          render_item=alembic_helpers.render_item)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
