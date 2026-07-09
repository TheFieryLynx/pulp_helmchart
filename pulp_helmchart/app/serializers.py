import os
from gettext import gettext as _
from tempfile import NamedTemporaryFile

from django.conf import settings
from django.db import DatabaseError
from rest_framework import serializers

from pulpcore.plugin import models
from pulpcore.plugin.files import PulpTemporaryUploadedFile
from pulpcore.plugin.serializers import (
    ArtifactSerializer,
    ContentChecksumSerializer,
    DetailRelatedField,
    DistributionSerializer,
    PublicationSerializer,
    RepositorySerializer,
    SingleArtifactContentUploadSerializer,
)
from pulpcore.plugin.util import get_domain_pk

from pulp_helmchart.helm import (
    HelmChartError,
    default_archive_filename,
    parse_chart_archive,
)

from .models import (
    HelmChartContent,
    HelmChartDistribution,
    HelmChartPublication,
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
        """Validate chart upload data and create/reuse an Artifact synchronously."""
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

        if file := data.pop("file", None):
            self._populate_chart_fields(data, file=file)
            try:
                artifact = models.Artifact.objects.get(
                    sha256=file.hashers["sha256"].hexdigest(), pulp_domain=get_domain_pk()
                )
                if not artifact.pulp_domain.get_storage().exists(artifact.file.name):
                    artifact.file = file
                    artifact.save()
                else:
                    artifact.touch()
            except (models.Artifact.DoesNotExist, DatabaseError):
                serializer = ArtifactSerializer(data={"file": file})
                serializer.is_valid(raise_exception=True)
                artifact = serializer.save()
            data["artifact"] = artifact
        else:
            self._populate_chart_fields(data)

        return data

    class Meta:
        fields = HelmChartContentSerializer.Meta.fields
        model = HelmChartContent
        ref_name = "HelmChartContentUploadSerializer"


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
