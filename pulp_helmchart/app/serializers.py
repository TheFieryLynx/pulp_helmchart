import os
from gettext import gettext as _
from rest_framework import serializers

from pulpcore.plugin import models
from pulpcore.plugin.files import PulpTemporaryUploadedFile
from pulpcore.plugin.models import UploadChunk
from pulpcore.plugin.serializers import (
    ContentChecksumSerializer,
    DetailRelatedField,
    DistributionSerializer,
    PublicationSerializer,
    RemoteSerializer,
    RepositorySerializer,
    RepositorySyncURLSerializer,
    RepositoryVersionRelatedField,
    SingleArtifactContentUploadSerializer,
)
from pulpcore.plugin.util import get_domain_pk

from pulp_helmchart.helm import (
    HelmChartError,
    default_archive_filename,
    parse_chart_archive,
)

from .content import validate_content_artifact_path
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
        upload = data.pop("upload", None)
        assembled = None
        if upload is not None:
            self.context["upload"] = upload
            assembled = PulpTemporaryUploadedFile(
                "chart.tgz", "application/gzip", upload.size, None
            )
            try:
                chunks = UploadChunk.objects.filter(upload=upload).order_by("offset")
                for chunk in chunks.iterator():
                    for part in chunk.file.chunks():
                        assembled.write(part)
                        for hasher in assembled.hashers.values():
                            hasher.update(part)
                assembled.seek(0)
                data["file"] = assembled
            except Exception:
                assembled.close()
                upload.delete()
                raise
        try:
            if assembled is not None:
                self._validate_chart_file(assembled)
            data = super().deferred_validate(data)
            self._populate_chart_fields(data)
            return data
        except Exception:
            if upload is not None:
                upload.delete()
            raise
        finally:
            if assembled is not None:
                assembled.close()

    def download(self, url, expected_digests=None, expected_size=None):
        file = super().download(url, expected_digests, expected_size)
        try:
            self._validate_chart_file(file)
        except Exception:
            file.close()
            raise
        return file

    def _validate_chart_file(self, file):
        try:
            parse_chart_archive(file)
        except HelmChartError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def create(self, validated_data):
        try:
            return super().create(validated_data)
        finally:
            upload = self.context.get("upload")
            if upload is not None and upload.pk is not None:
                upload.delete()

    def retrieve(self, validated_data):
        """Return existing identical chart content, or reject immutable chart replacement."""
        existing = HelmChartContent.objects.filter(
            name=validated_data["name"],
            version=validated_data["version"],
            _pulp_domain=get_domain_pk(),
        )
        same_digest = existing.filter(digest=validated_data["digest"]).first()
        if same_digest:
            validate_content_artifact_path(
                same_digest, self._requested_relative_path, validated_data["digest"]
            )
            return same_digest
        if existing.exists():
            raise serializers.ValidationError(
                _(
                    "Chart '{name}' version '{version}' already exists with a different digest."
                ).format(name=validated_data["name"], version=validated_data["version"])
            )
        return None

    def get_artifacts(self, validated_data):
        self._requested_relative_path = validated_data["relative_path"]
        return super().get_artifacts(validated_data)

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
    """Chart upload request; creation runs in Pulp's reserved content task."""

    class Meta:
        fields = HelmChartContentSerializer.Meta.fields
        model = HelmChartContent
        ref_name = "HelmChartContentUploadSerializer"


class AutoExcludedVersionSerializer(serializers.Serializer):
    """Diagnostic metadata retained until explicitly removed from a remote."""

    reason = serializers.ChoiceField(choices=["checksum_mismatch"])
    expected = serializers.CharField()
    actual = serializers.CharField()
    url = serializers.CharField()
    timestamp = serializers.DateTimeField()

    def validate_timestamp(self, value):
        return value.isoformat()


class RetryAutoExclusionSerializer(serializers.Serializer):
    """Identify exactly one automatic exclusion to remove."""

    chart = serializers.CharField(allow_blank=False)
    version = serializers.CharField(allow_blank=False)


class HelmChartRemoteSerializer(RemoteSerializer):
    """
    Serializer for classic Helm chart remotes.
    """

    policy = serializers.ChoiceField(
        help_text=_(
            "Helm chart archives are downloaded during sync; only immediate is supported."
        ),
        choices=[(models.Remote.IMMEDIATE, models.Remote.IMMEDIATE)],
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
        help_text=_(
            "Optional list of chart names to skip after include_charts is applied."
        ),
    )
    allowed_chart_hosts = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
        default=list,
        help_text=_(
            "Exact archive hostnames allowed in addition to the remote origin. "
            "Remote credentials and custom headers are never sent to these hosts."
        ),
    )
    include_versions = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        required=False,
        default=dict,
        help_text=_(
            "Chart names mapped to exact versions to sync. '*' is a global fallback overridden "
            "by chart-specific entries. Without either key, all versions are eligible; "
            "an empty list selects no versions."
        ),
    )
    exclude_versions = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        required=False,
        default=dict,
        help_text=_(
            "Chart names mapped to exact versions to skip after include_versions. "
            "'*' exclusions are combined with chart-specific exclusions."
        ),
    )
    checksum_mismatch_policy = serializers.ChoiceField(
        choices=HelmChartRemote.CHECKSUM_MISMATCH_POLICIES,
        required=False,
        default="fail",
        help_text=_(
            "On checksum mismatch: fail, skip this sync, or exclude from future syncs."
        ),
    )
    auto_excluded_versions = serializers.DictField(
        child=serializers.DictField(child=AutoExcludedVersionSerializer()),
        required=False,
        default=dict,
        help_text=_(
            "Automatic exclusions keyed by chart name and version with checksum diagnostics. "
            "Use retry_auto_exclusion for a targeted retry; PATCH with {} to clear all."
        ),
    )
    latest_only = serializers.BooleanField(
        required=False,
        default=False,
        help_text=_(
            "If true, sync the highest semantic version for each selected chart after filters."
        ),
    )
    ignore_unavailable = serializers.BooleanField(
        required=False,
        default=True,
        help_text=_("If true, skip chart archives that return HTTP 403, 404, or 410."),
    )

    def validate_allowed_chart_hosts(self, hosts):
        from pulp_helmchart.helm import normalize_chart_host

        try:
            return list(dict.fromkeys(normalize_chart_host(host) for host in hosts))
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    class Meta:
        fields = RemoteSerializer.Meta.fields + (
            "include_charts",
            "exclude_charts",
            "allowed_chart_hosts",
            "include_versions",
            "exclude_versions",
            "checksum_mismatch_policy",
            "auto_excluded_versions",
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
                {
                    "mirror": _(
                        "Mirror sync is not implemented for Helm chart repositories yet."
                    )
                }
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

    repository = DetailRelatedField(
        required=False,
        view_name_pattern=r"repositories(-.*/.*)?-detail",
        queryset=HelmChartRepository.objects.all(),
    )
    repository_version = RepositoryVersionRelatedField(
        required=False,
        queryset=models.RepositoryVersion.objects.filter(
            repository__in=HelmChartRepository.objects.all()
        ),
    )
    distributions = DetailRelatedField(
        help_text=_("This publication is currently hosted by these distributions."),
        source="distribution_set",
        view_name="helmchartdistributions-detail",
        many=True,
        read_only=True,
    )
    index = serializers.ChoiceField(
        help_text=_(
            "The generated Helm repository index is always at publication root."
        ),
        choices=[("index.yaml", "index.yaml")],
        default="index.yaml",
        required=False,
    )
    checkpoint = serializers.BooleanField(required=False)

    class Meta:
        model = HelmChartPublication
        fields = PublicationSerializer.Meta.fields + (
            "distributions",
            "index",
            "checkpoint",
        )


class HelmChartDistributionSerializer(DistributionSerializer):
    """
    Serializer for Helm chart distributions.
    """

    repository = DetailRelatedField(
        required=False,
        allow_null=True,
        view_name_pattern=r"repositories(-.*/.*)?-detail",
        queryset=HelmChartRepository.objects.all(),
    )
    repository_version = RepositoryVersionRelatedField(
        required=False,
        allow_null=True,
        queryset=models.RepositoryVersion.objects.filter(
            repository__in=HelmChartRepository.objects.all()
        ),
    )
    publication = DetailRelatedField(
        required=False,
        help_text=_("Publication to be served"),
        view_name_pattern=r"publications(-.*/.*)?-detail",
        queryset=HelmChartPublication.objects.exclude(complete=False),
        allow_null=True,
    )
    checkpoint = serializers.BooleanField(required=False)

    class Meta:
        fields = DistributionSerializer.Meta.fields + ("publication", "checkpoint")
        model = HelmChartDistribution


def _uploaded_filename(file) -> str:
    name = getattr(file, "name", "") or ""
    return os.path.basename(name)
