"""Prefilter summary references and remove guards with an always-empty result scope."""

import sqlalchemy as sa
from alembic import op

from axile.server.alembic.business0037_invalidation import drop_summary_guards, install_summary_guards
from axile.server.alembic.business0037_schema import TABLE_NAMES

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def _replace_summary_guards(*, optimize: bool) -> None:
    """Replace only summary guards atomically using the frozen 0037 registry."""
    connection = op.get_bind()
    metadata = sa.MetaData()
    tables = tuple(sa.Table(name, metadata, autoload_with=connection) for name in TABLE_NAMES)
    with connection.begin_nested():
        drop_summary_guards(connection)
        install_summary_guards(connection, tables, optimize=optimize)


def upgrade() -> None:
    """Install conservative text prefilters without changing business evidence."""
    _replace_summary_guards(optimize=True)


def downgrade() -> None:
    """Restore the original unfiltered 0037 definitions, including empty guards."""
    _replace_summary_guards(optimize=False)
