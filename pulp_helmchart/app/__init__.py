from pulpcore.plugin import PulpPluginAppConfig


class PulpHelmChartPluginAppConfig(PulpPluginAppConfig):
    """
    Entry point for the pulp_helmchart plugin.
    """

    name = "pulp_helmchart.app"
    label = "helmchart"
    version = "0.1.0.dev0"
    python_package_name = "pulp-helmchart"
    domain_compatible = True
