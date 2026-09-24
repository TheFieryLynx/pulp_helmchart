import logging
import os
import tempfile
from gettext import gettext as _

from django.conf import settings
from django.core.files import File

from pulpcore.plugin.models import (
    ContentArtifact,
    PublishedMetadata,
    RemoteArtifact,
    RepositoryVersion,
)

from pulp_helmchart.helm import build_index_entry, index_from_entries, utc_timestamp

from ..models import HelmChartPublication, HelmChartRepository
from ..serializers import HelmChartPublicationSerializer
from .progress import task_progress

log = logging.getLogger(__name__)


def publish(
    repository_version_pk,
    index="index.yaml",
    checkpoint=False,
    record_created_resource=True,
):
    """
    Create a Helm chart publication from a repository version.

    Args:
        repository_version_pk (str): RepositoryVersion primary key to publish.
        index (str): Relative path for the generated Helm index.
        checkpoint (bool): Whether to create a checkpoint publication.
        record_created_resource (bool): Whether to register the publication on the current task.
    """
    if index != "index.yaml":
        raise ValueError("Helm publication index must be at root as 'index.yaml'.")
    repo_version = RepositoryVersion.objects.get(pk=repository_version_pk)
    if not HelmChartRepository.objects.filter(pk=repo_version.repository_id).exists():
        raise ValueError("Helm publications require a Helm chart repository version.")

    log.info(
        _(
            "Publishing Helm chart repository={repo}, version={ver}, index={index}"
        ).format(
            repo=repo_version.repository.name, ver=repo_version.number, index=index
        )
    )

    with tempfile.TemporaryDirectory(dir=settings.WORKING_DIRECTORY) as temp_dir:
        with _create_publication(
            repo_version,
            pass_through=True,
            checkpoint=checkpoint,
            record_created_resource=record_created_resource,
        ) as publication:
            publication.index = index
            _write_index_file(index, repo_version, publication, temp_dir)

        log.info(
            _("Publication: {publication} created").format(publication=publication.pk)
        )

        publication = HelmChartPublicationSerializer(
            instance=publication, context={"request": None}
        ).data

        return publication


def _create_publication(
    repo_version, pass_through, checkpoint, record_created_resource
):
    if record_created_resource:
        return HelmChartPublication.create(
            repo_version, pass_through=pass_through, checkpoint=checkpoint
        )

    publication = HelmChartPublication(
        pass_through=pass_through,
        repository_version=repo_version,
        checkpoint=checkpoint,
    )
    publication.save()
    return publication


def _write_index_file(index, repo_version, publication, temp_dir):
    generated = utc_timestamp()
    index_path = _metadata_path(temp_dir, index)
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    with open(index_path, "w+", encoding="utf-8") as index_file:
        index_file.write(
            index_from_entries(
                yield_index_entries_for_version(repo_version, generated),
                generated=generated,
            )
        )
    with open(index_path, "rb") as metadata_file:
        PublishedMetadata.create_from_file(
            file=File(metadata_file),
            publication=publication,
            relative_path=index,
        )


def _metadata_path(temp_dir, relative_path):
    if os.path.isabs(relative_path) or ".." in relative_path.split("/"):
        raise ValueError(
            "Publication metadata path must be relative and cannot contain '..'."
        )
    return os.path.join(temp_dir, relative_path)


def yield_index_entries_for_version(repo_version, generated):
    """
    Yield Helm index entries for every chart archive in a repository version.
    """
    content_artifacts = ContentArtifact.objects.filter(
        content__in=repo_version.content
    ).order_by("relative_path")

    seen_content = set()
    with task_progress(
        "Generating Helm index",
        "helmchart.publish_index",
        total=content_artifacts.count(),
    ) as progress:
        for content_artifact in progress.iter(
            content_artifacts.select_related(
                "artifact", "content__helmchart_helmchartcontent"
            ).iterator(chunk_size=1000)
        ):
            chart = content_artifact.content.helmchart_helmchartcontent
            if (
                chart.pk in seen_content
                or content_artifact.relative_path != chart.filename
            ):
                raise ValueError(
                    f"Chart content {chart.pk} has ambiguous archive paths; repair it before "
                    "publishing."
                )
            seen_content.add(chart.pk)
            if content_artifact.artifact:
                artifact = content_artifact.artifact
            else:
                artifact = RemoteArtifact.objects.filter(
                    content_artifact=content_artifact
                ).first()

            yield build_index_entry(
                name=chart.name,
                version=chart.version,
                api_version=chart.api_version,
                app_version=chart.app_version,
                description=chart.description,
                annotations=chart.chart_yaml.get("annotations") or None,
                filename=content_artifact.relative_path,
                digest=artifact.sha256,
                created=utc_timestamp(chart.pulp_created),
                chart_yaml=chart.chart_yaml,
            )
