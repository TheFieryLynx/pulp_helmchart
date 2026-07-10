from logging import getLogger

from django.db import models

from pulpcore.plugin.models import (
    AutoAddObjPermsMixin,
    Content,
    Distribution,
    Publication,
    Remote,
    Repository,
)
from pulpcore.plugin.publication_utils import validate_publication_paths
from pulpcore.plugin.repo_version_utils import remove_duplicates, validate_repo_version
from pulpcore.plugin.util import get_domain_pk

log = getLogger(__name__)


class HelmChartContent(Content):
    """
    A packaged Helm chart archive.

    Content of this type represents one immutable chart ``name`` + ``version`` + digest.
    The original ``.tgz`` archive is stored and published as-is.
    """

    PROTECTED_FROM_RECLAIM = False

    TYPE = "chart"
    repo_key_fields = ("name", "version")

    name = models.TextField(null=False)
    version = models.TextField(null=False)
    api_version = models.TextField(null=True)
    app_version = models.TextField(null=True)
    description = models.TextField(null=True)
    digest = models.CharField(max_length=64, null=False)
    filename = models.TextField(null=False)
    chart_yaml = models.JSONField(default=dict)
    _pulp_domain = models.ForeignKey("core.Domain", default=get_domain_pk, on_delete=models.PROTECT)

    class Meta:
        default_related_name = "%(app_label)s_%(model_name)s"
        unique_together = ("name", "version", "digest", "_pulp_domain")
        permissions = [
            ("upload_helmchart", "Can upload Helm chart content using synchronous API."),
        ]


class HelmChartRemote(Remote, AutoAddObjPermsMixin):
    """
    Remote for classic Helm chart repositories.
    """

    TYPE = "helmchart"

    include_charts = models.JSONField(default=list)
    exclude_charts = models.JSONField(default=list)
    include_versions = models.JSONField(default=list)
    exclude_versions = models.JSONField(default=list)
    latest_only = models.BooleanField(default=False)
    ignore_unavailable = models.BooleanField(default=True)

    class Meta:
        default_related_name = "%(app_label)s_%(model_name)s"
        permissions = [
            ("manage_roles_helmchartremote", "Can manage roles on Helm chart remotes"),
        ]


class HelmChartRepository(Repository, AutoAddObjPermsMixin):
    """
    Repository type for classic Helm chart repositories.
    """

    TYPE = "helmchart"
    CONTENT_TYPES = [HelmChartContent]
    REMOTE_TYPES = [HelmChartRemote]

    autopublish = models.BooleanField(default=False)
    last_sync_details = models.JSONField(default=dict)

    class Meta:
        default_related_name = "%(app_label)s_%(model_name)s"
        permissions = [
            ("modify_helmchartrepository", "Can modify content of the Helm chart repository"),
            ("sync_helmchartrepository", "Can sync the Helm chart repository"),
            ("manage_roles_helmchartrepository", "Can manage roles on Helm chart repositories"),
            ("repair_helmchartrepository", "Can repair repository versions"),
        ]

    def on_new_version(self, version):
        """
        Publish automatically when configured to do so.
        """
        super().on_new_version(version)

        from pulp_helmchart.app import tasks

        if self.autopublish:
            tasks.publish(repository_version_pk=version.pk, record_created_resource=False)

    def finalize_new_version(self, new_version):
        """
        Validate duplicate chart versions and overlapping published paths.
        """
        remove_duplicates(new_version)
        validate_repo_version(new_version)


class HelmChartPublication(Publication, AutoAddObjPermsMixin):
    """
    Publication for classic Helm chart content.
    """

    TYPE = "helmchart"

    index = models.TextField(default="index.yaml", null=False)

    class Meta:
        default_related_name = "%(app_label)s_%(model_name)s"
        permissions = [
            ("manage_roles_helmchartpublication", "Can manage roles on Helm chart publications"),
        ]

    def finalize_new_publication(self):
        """
        Validate that artifact and metadata paths do not overlap.
        """
        validate_publication_paths(self)


class HelmChartDistribution(Distribution, AutoAddObjPermsMixin):
    """
    Distribution for classic Helm chart repositories.
    """

    TYPE = "helmchart"
    SERVE_FROM_PUBLICATION = True

    class Meta:
        default_related_name = "%(app_label)s_%(model_name)s"
        permissions = [
            ("manage_roles_helmchartdistribution", "Can manage roles on Helm chart distributions"),
        ]
