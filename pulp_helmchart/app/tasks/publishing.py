import logging
import tempfile
from gettext import gettext as _

from django.core.files import File

from pulpcore.plugin.models import ContentArtifact, PublishedMetadata, RemoteArtifact, RepositoryVersion

from pulp_helmchart.helm import build_index_entry, index_from_entries, utc_timestamp

from ..models import HelmChartPublication
from ..serializers import HelmChartPublicationSerializer

log = logging.getLogger(__name__)


def publish(repository_version_pk, index="index.yaml", checkpoint=False):
    """
    Create a Helm chart publication from a repository version.

    Args:
        repository_version_pk (str): RepositoryVersion primary key to publish.
        index (str): Relative path for the generated Helm index.
        checkpoint (bool): Whether to create a checkpoint publication.
    """
    repo_version = RepositoryVersion.objects.get(pk=repository_version_pk)

    log.info(
        _("Publishing Helm chart repository={repo}, version={ver}, index={index}").format(
            repo=repo_version.repository.name, ver=repo_version.number, index=index
        )
    )

    with tempfile.TemporaryDirectory(dir="."):
        with HelmChartPublication.create(
            repo_version, pass_through=True, checkpoint=checkpoint
        ) as publication:
            publication.index = index
            _write_index_file(index, repo_version, publication)

        log.info(_("Publication: {publication} created").format(publication=publication.pk))

        publication = HelmChartPublicationSerializer(
            instance=publication, context={"request": None}
        ).data

        return publication


def _write_index_file(index, repo_version, publication):
    generated = utc_timestamp()
    entries = list(yield_index_entries_for_version(repo_version, generated))
    with open(index, "w+", encoding="utf-8") as index_file:
        index_file.write(index_from_entries(entries, generated=generated))
        index_file.flush()

    PublishedMetadata.create_from_file(
        file=File(open(index, "rb")),
        publication=publication,
        relative_path=index,
    )


def yield_index_entries_for_version(repo_version, generated):
    """
    Yield Helm index entries for every chart archive in a repository version.
    """
    content_artifacts = ContentArtifact.objects.filter(content__in=repo_version.content).order_by(
        "relative_path"
    )

    for content_artifact in content_artifacts.select_related("artifact", "content").iterator():
        chart = content_artifact.content.cast()
        if content_artifact.artifact:
            artifact = content_artifact.artifact
        else:
            artifact = RemoteArtifact.objects.filter(content_artifact=content_artifact).first()

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
