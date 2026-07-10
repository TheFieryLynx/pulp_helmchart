import os
from dataclasses import dataclass
from gettext import gettext as _

from django.db import DatabaseError, IntegrityError, transaction
from rest_framework import serializers

from pulpcore.plugin import models
from pulpcore.plugin.serializers import ArtifactSerializer
from pulpcore.plugin.util import get_domain_pk

from pulp_helmchart.helm import (
    HelmChartError,
    default_archive_filename,
    parse_chart_archive,
    sha256_file,
)

from .models import HelmChartContent


@dataclass(frozen=True)
class HelmChartContentResult:
    """Created or reused Helm chart content plus its artifact path."""

    content: HelmChartContent
    artifact: models.Artifact
    relative_path: str
    created: bool


def create_helmchart_content_from_tgz(file, relative_path=None) -> HelmChartContentResult:
    """
    Validate a packaged Helm chart archive and create/reuse its Pulp content records.

    Args:
        file: File-like object containing the original chart ``.tgz``.
        relative_path: Optional path to publish the chart at. Defaults to the archive filename.

    Raises:
        serializers.ValidationError: If the archive is invalid or tries to replace an immutable
            chart version with different bytes.
    """
    if file is None:
        raise serializers.ValidationError(_("A Helm chart archive file is required."))

    try:
        metadata = parse_chart_archive(file)
    except HelmChartError as exc:
        raise serializers.ValidationError(str(exc)) from exc

    digest = _sha256(file)
    filename = os.path.basename(relative_path or _uploaded_filename(file))
    if not filename:
        filename = default_archive_filename(metadata.name, metadata.version)

    artifact = _get_or_create_artifact(file, digest)
    domain_pk = get_domain_pk()
    existing = HelmChartContent.objects.filter(
        name=metadata.name,
        version=metadata.version,
        _pulp_domain=domain_pk,
    )
    content = existing.filter(digest=digest).first()
    created = False

    if content is None and existing.exists():
        raise serializers.ValidationError(
            _(
                "Chart '{name}' version '{version}' already exists with a different digest."
            ).format(name=metadata.name, version=metadata.version)
        )

    if content is None:
        try:
            with transaction.atomic():
                content = HelmChartContent.objects.create(
                    name=metadata.name,
                    version=metadata.version,
                    api_version=metadata.api_version,
                    app_version=metadata.app_version,
                    description=metadata.description,
                    digest=digest,
                    filename=filename,
                    chart_yaml=metadata.chart_yaml,
                )
                models.ContentArtifact.objects.create(
                    artifact=artifact, content=content, relative_path=filename
                )
        except IntegrityError:
            content = existing.filter(digest=digest).first()
            if content is None:
                raise
        else:
            created = True

    if not created:
        content.touch()
        models.ContentArtifact.objects.update_or_create(
            content=content,
            relative_path=filename,
            defaults={"artifact": artifact},
        )

    return HelmChartContentResult(
        content=content,
        artifact=artifact,
        relative_path=filename,
        created=created,
    )


def _get_or_create_artifact(file, digest):
    try:
        artifact = models.Artifact.objects.get(sha256=digest, pulp_domain=get_domain_pk())
        if not artifact.pulp_domain.get_storage().exists(artifact.file.name):
            artifact.file = file
            artifact.save()
        else:
            artifact.touch()
    except (models.Artifact.DoesNotExist, DatabaseError):
        serializer = ArtifactSerializer(data={"file": file})
        serializer.is_valid(raise_exception=True)
        artifact = serializer.save()
    return artifact


def _sha256(file):
    hashers = getattr(file, "hashers", None)
    if hashers and "sha256" in hashers:
        return hashers["sha256"].hexdigest()
    return sha256_file(file)


def _uploaded_filename(file) -> str:
    name = getattr(file, "name", "") or ""
    return os.path.basename(name)
