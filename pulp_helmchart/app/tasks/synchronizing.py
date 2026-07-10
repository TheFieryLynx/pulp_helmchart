import os
from gettext import gettext as _
from urllib.parse import unquote, urlparse

from django.utils import timezone

from pulpcore.plugin.files import PulpTemporaryUploadedFile
from pulpcore.plugin.models import RepositoryVersion
from pulpcore.plugin.serializers import RepositoryVersionSerializer

from pulp_helmchart.helm import (
    HelmChartError,
    default_archive_filename,
    parse_repository_index,
    repository_index_url,
    resolve_chart_url,
    sha256_file,
    verify_sha256_digest,
)

from ..content import create_helmchart_content_from_tgz
from ..models import HelmChartContent, HelmChartRemote, HelmChartRepository


def synchronize(remote_pk, repository_pk):
    """
    Sync content from a classic Helm chart repository remote.

    This first implementation is additive: existing repository content remains in the new
    repository version, and charts discovered upstream are added or reused.
    """
    remote = HelmChartRemote.objects.get(pk=remote_pk)
    repository = HelmChartRepository.objects.get(pk=repository_pk)

    if not remote.url:
        raise ValueError(_("A remote must have a url specified to synchronize."))

    index_url = repository_index_url(remote.url)
    index_result = remote.get_downloader(url=index_url).fetch()
    with open(index_result.path, "rb") as index_file:
        all_entries = parse_repository_index(index_file)
    entries = _filter_entries(all_entries, remote)
    if not entries:
        raise ValueError(_("No Helm chart entries matched the remote sync filters."))

    synced_content = []
    downloaded = 0
    reused = 0
    index_digest = _sha256_path(index_result.path)

    with repository.new_version() as new_version:
        for entry in entries:
            chart_url = resolve_chart_url(remote.url, entry.urls[0])
            filename = _chart_filename(chart_url, entry.chart_name, entry.version)
            chart_result = remote.get_downloader(url=chart_url).fetch()
            chart_digest = _sha256_path(chart_result.path)
            verify_sha256_digest(entry.digest, chart_digest, chart_url)

            with open(chart_result.path, "rb") as chart_file:
                uploaded = PulpTemporaryUploadedFile.from_file(chart_file)
                result = create_helmchart_content_from_tgz(uploaded, relative_path=filename)

            if result.content.name != entry.chart_name or result.content.version != entry.version:
                raise HelmChartError(
                    "Chart archive metadata does not match upstream index entry: "
                    f"index has {entry.chart_name!r} {entry.version!r}, archive has "
                    f"{result.content.name!r} {result.content.version!r}."
                )

            if result.created:
                downloaded += 1
            else:
                reused += 1
            synced_content.append(result.content.pk)

        if synced_content:
            new_version.add_content(HelmChartContent.objects.filter(pk__in=synced_content))

    latest_version = repository.latest_version()
    repository.last_sync_details = {
        "remote_pk": str(remote.pk),
        "url": remote.url,
        "index_url": index_url,
        "index_checksum": index_digest,
        "synced_at": timezone.now().isoformat(),
        "charts_seen": len(entries),
        "charts_available": len(all_entries),
        "charts_added_or_updated": len(synced_content),
        "charts_created": downloaded,
        "charts_reused": reused,
        "sync_mode": "additive",
        "most_recent_version": latest_version.number,
    }
    repository.save()

    repo_version = RepositoryVersion.objects.filter(pk=latest_version.pk).first()
    if repo_version:
        return RepositoryVersionSerializer(instance=repo_version, context={"request": None}).data
    return None


def _chart_filename(url, chart_name, version):
    filename = os.path.basename(unquote(urlparse(url).path))
    return filename or default_archive_filename(chart_name, version)


def _filter_entries(entries, remote):
    include_charts = set(remote.include_charts or [])
    include_versions = set(remote.include_versions or [])
    selected = [
        entry
        for entry in entries
        if (not include_charts or entry.chart_name in include_charts)
        and (not include_versions or entry.version in include_versions)
    ]

    if not remote.latest_only:
        return selected

    latest = []
    seen = set()
    for entry in selected:
        if entry.chart_name in seen:
            continue
        seen.add(entry.chart_name)
        latest.append(entry)
    return latest


def _sha256_path(path):
    with open(path, "rb") as file:
        return sha256_file(file)
