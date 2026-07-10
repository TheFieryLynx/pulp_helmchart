import os
from gettext import gettext as _
from tempfile import NamedTemporaryFile

from django.conf import settings
from rest_framework import serializers

from pulpcore.plugin import models
from pulpcore.plugin.files import PulpTemporaryUploadedFile
from pulpcore.plugin.serializers import (
    ContentChecksumSerializer,
    DetailRelatedField,
    DistributionSerializer,
    PublicationSerializer,
    RemoteSerializer,
    RepositorySerializer,
    RepositorySyncURLSerializer,
    SingleArtifactContentUploadSerializer,
)
from pulpcore.plugin.util import get_domain_pk

from pulp_helmchart.helm import (
    HelmChartError,
    default_archive_filename,
    parse_chart_archive,
)

from .content import create_helmchart_content_from_tgz
from .models import (
    HelmChartContent,
    HelmChartDistribution,
    HelmChartPublication,
    HelmChartRemote,
    HelmChartRepository,
)


class HelmChartContentSerializer(
    SingleArtifactContentUploadSerializer, ContentChecksumSerializer
):
    """
    Serializer for packaged Helm chart content.
    """

    name = serializers.CharField(read_only=True)
    version = serializers.CharField(read_only=True)
    api_version = serializers.CharField(read_only=True, allow_null=True)
    app_version = serializers.CharField(read_only=True, allow_null=True)
    description = serializers.CharField(read_only=True, allow_null=True)
    digest = serializers.CharField(read_only=True)
    filename = serializers.CharField(read_only=True)
    chart_yaml = serializers.JSONField(read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["relative_path"].required = False

    def deferred_validate(self, data):
        """Validate chart metadata after the uploaded file has become an Artifact."""
        data = super().deferred_validate(data)
        self._populate_chart_fields(data)
        return data

    def retrieve(self, validated_data):
        """Return existing identical chart content, or reject immutable chart replacement."""
        existing = HelmChartContent.objects.filter(
            name=validated_data["name"],
            version=validated_data["version"],
            _pulp_domain=get_domain_pk(),
        )
        same_digest = existing.filter(digest=validated_data["digest"]).first()
        if same_digest:
            return same_digest
        if existing.exists():
            raise serializers.ValidationError(
                _(
                    "Chart '{name}' version '{version}' already exists with a different digest."
                ).format(name=validated_data["name"], version=validated_data["version"])
            )
        return None

    def _populate_chart_fields(self, data, file=None):
        artifact = data.get("artifact")
        close_after = False
        if file is None and artifact is not None:
            file = artifact.file
            file.open("rb")
            close_after = True
        if file is None:
            return

        try:
            metadata = parse_chart_archive(file)
        except HelmChartError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        finally:
            if close_after:
                file.close()

        digest = (
            artifact.sha256
            if artifact is not None
            else file.hashers["sha256"].hexdigest()
        )
        filename = data.get("relative_path") or _uploaded_filename(file)
        if not filename:
            filename = default_archive_filename(metadata.name, metadata.version)
        filename = os.path.basename(filename)

        data.update(
            {
                "name": metadata.name,
                "version": metadata.version,
                "api_version": metadata.api_version,
                "app_version": metadata.app_version,
                "description": metadata.description,
                "digest": digest,
                "filename": filename,
                "relative_path": filename,
                "chart_yaml": metadata.chart_yaml,
            }
        )

    class Meta:
        fields = (
            SingleArtifactContentUploadSerializer.Meta.fields
            + ContentChecksumSerializer.Meta.fields
            + (
                "name",
                "version",
                "api_version",
                "app_version",
                "description",
                "digest",
                "filename",
                "chart_yaml",
            )
        )
        model = HelmChartContent


class HelmChartContentUploadSerializer(HelmChartContentSerializer):
    """
    Synchronous upload serializer for packaged Helm charts.
    """

    def validate(self, data):
        """Validate chart upload data and prepare the archive file for synchronous creation."""
        data = super().validate(data)

        if upload := data.pop("upload", None):
            chunks = models.UploadChunk.objects.filter(upload=upload).order_by("offset")
            with NamedTemporaryFile(
                mode="ab", dir=settings.WORKING_DIRECTORY, delete=False
            ) as temp_file:
                for chunk in chunks:
                    temp_file.write(chunk.file.read())
                    chunk.file.close()
                temp_file.flush()
            data["file"] = PulpTemporaryUploadedFile.from_file(open(temp_file.name, "rb"))
        elif file_url := data.pop("file_url", None):
            expected_digests = data.get("expected_digests", None)
            expected_size = data.get("expected_size", None)
            data["file"] = self.download(
                file_url, expected_digests=expected_digests, expected_size=expected_size
            )

        return data

    def create(self, validated_data):
        """Create or reuse chart content through the shared Helm chart helper."""
        repository = validated_data.pop("repository", None)
        relative_path = validated_data.pop("relative_path", None)
        file = validated_data.pop("file", None)
        artifact = validated_data.pop("artifact", None)

        close_after = False
        if file is None and artifact is not None:
            file = artifact.file
            file.open("rb")
            close_after = True
        try:
            result = create_helmchart_content_from_tgz(file, relative_path=relative_path)
        finally:
            if close_after:
                file.close()

        if repository:
            repository = repository.cast()
            with repository.new_version() as new_version:
                new_version.add_content(HelmChartContent.objects.filter(pk=result.content.pk))

        return result.content

    class Meta:
        fields = HelmChartContentSerializer.Meta.fields
        model = HelmChartContent
        ref_name = "HelmChartContentUploadSerializer"


class HelmChartRemoteSerializer(RemoteSerializer):
    """
    Serializer for classic Helm chart remotes.
    """

    policy = serializers.ChoiceField(
        help_text=_(
            "The policy to use when downloading content. For the first sync implementation "
            "charts are downloaded immediately."
        ),
        choices=models.Remote.POLICY_CHOICES,
        default=models.Remote.IMMEDIATE,
    )
    include_charts = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text=_("Optional list of chart names to sync. Empty means all charts."),
    )
    exclude_charts = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text=_("Optional list of chart names to skip after include_charts is applied."),
    )
    include_versions = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text=_("Optional list of chart versions to sync. Empty means all versions."),
    )
    latest_only = serializers.BooleanField(
        required=False,
        default=False,
        help_text=_("If true, sync only the first index entry for each selected chart."),
    )
    ignore_unavailable = serializers.BooleanField(
        required=False,
        default=True,
        help_text=_("If true, skip chart archives that return HTTP 403, 404, or 410."),
    )

    class Meta:
        fields = RemoteSerializer.Meta.fields + (
            "include_charts",
            "exclude_charts",
            "include_versions",
            "latest_only",
            "ignore_unavailable",
        )
        model = HelmChartRemote


class HelmChartRepositorySyncURLSerializer(RepositorySyncURLSerializer):
    """
    Serializer for Helm chart repository sync requests.
    """

    mirror = serializers.BooleanField(required=False, default=False)

    def validate(self, data):
        data = super().validate(data)
        if data.get("mirror"):
            raise serializers.ValidationError(
                {"mirror": _("Mirror sync is not implemented for Helm chart repositories yet.")}
            )
        return data


class HelmChartRepositorySerializer(RepositorySerializer):
    """
    Serializer for Helm chart repositories.
    """

    autopublish = serializers.BooleanField(
        help_text=_(
            "Whether to automatically create Helm chart publications for new repository versions."
        ),
        default=False,
        required=False,
    )
    last_sync_details = serializers.JSONField(
        help_text=_("Details about the last sync of this repository."),
        read_only=True,
    )

    class Meta:
        fields = RepositorySerializer.Meta.fields + ("autopublish", "last_sync_details")
        model = HelmChartRepository


class HelmChartPublicationSerializer(PublicationSerializer):
    """
    Serializer for Helm chart publications.
    """

    distributions = DetailRelatedField(
        help_text=_("This publication is currently hosted by these distributions."),
        source="distribution_set",
        view_name="helmchartdistributions-detail",
        many=True,
        read_only=True,
    )
    index = serializers.CharField(
        help_text=_("Filename for the generated Helm repository index."),
        default="index.yaml",
        required=False,
    )
    checkpoint = serializers.BooleanField(required=False)

    class Meta:
        model = HelmChartPublication
        fields = PublicationSerializer.Meta.fields + ("distributions", "index", "checkpoint")


class HelmChartDistributionSerializer(DistributionSerializer):
    """
    Serializer for Helm chart distributions.
    """

    publication = DetailRelatedField(
        required=False,
        help_text=_("Publication to be served"),
        view_name_pattern=r"publications(-.*/.*)?-detail",
        queryset=models.Publication.objects.exclude(complete=False),
        allow_null=True,
    )
    checkpoint = serializers.BooleanField(required=False)

    class Meta:
        fields = DistributionSerializer.Meta.fields + ("publication", "checkpoint")
        model = HelmChartDistribution


def _uploaded_filename(file) -> str:
    name = getattr(file, "name", "") or ""
    return os.path.basename(name)
