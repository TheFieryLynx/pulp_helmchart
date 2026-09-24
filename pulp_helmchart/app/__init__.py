from pulpcore.plugin import PulpPluginAppConfig


class PulpHelmChartPluginAppConfig(PulpPluginAppConfig):
    """
    Entry point for the pulp_helmchart plugin.
    """

    name = "pulp_helmchart.app"
    label = "helmchart"
    version = "0.2.0"
    python_package_name = "pulp-helmchart"
    domain_compatible = True

    def ready(self):
        super().ready()
        from django.db.models.signals import pre_migrate

        from .upgrade import prevent_mixed_version_migration

        pre_migrate.connect(
            prevent_mixed_version_migration,
            sender=self,
            dispatch_uid="helmchart_pre_0006_worker_guard",
        )
