"""Guard the already-shipped version-filter data migration during upgrades."""

from django.db import connections
from django.db.migrations.recorder import MigrationRecorder

from pulpcore.app.models import AppStatus


def prevent_mixed_version_migration(sender, using, **_kwargs):
    """Refuse 0006 while any old Pulp process can still read converted filters."""
    if sender.label != "helmchart":
        return
    connection = connections[using]
    recorder = MigrationRecorder(connection)
    if not recorder.has_table():
        return
    if ("helmchart", "0006_remote_sync_policies") in recorder.applied_migrations():
        return
    if AppStatus._meta.db_table not in connection.introspection.table_names():
        return
    if AppStatus.objects.online().using(using).exists():
        raise RuntimeError(
            "Helmchart migration 0006 converts version filters to mappings. Stop all Pulp "
            "API, content, and worker processes and wait for their status heartbeats to "
            "expire before running migrate. Do not restart old processes afterward."
        )
