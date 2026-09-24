import copy
import logging
import os
from contextlib import ExitStack
from gettext import gettext as _
from urllib.parse import unquote, urlparse, urlsplit

import aiohttp
from django.db import transaction
from django.utils import timezone

from pulpcore.plugin.download import DownloaderFactory
from pulpcore.exceptions import TimeoutException
from pulpcore.plugin.files import PulpTemporaryUploadedFile
from pulpcore.plugin.models import RepositoryVersion
from pulpcore.plugin.serializers import RepositoryVersionSerializer

from pulp_helmchart.helm import (
    HelmChartError,
    _http_origin,
    default_archive_filename,
    filter_repository_entries,
    parse_repository_index,
    redact_url,
    repository_index_url,
    sha256_file,
    validate_chart_url,
    verify_sha256_digest,
)

from ..content import create_helmchart_content_from_tgz
from ..models import HelmChartContent, HelmChartRemote, HelmChartRepository
from .progress import task_progress


log = logging.getLogger(__name__)

UNAVAILABLE_HTTP_STATUSES = {403, 404, 410}


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
    if remote.policy != remote.IMMEDIATE:
        raise HelmChartError(
            f"Helm remote policy {remote.policy!r} is unsupported; use 'immediate'."
        )

    index_url = repository_index_url(remote.url)
    with task_progress(
        "Parsing upstream Helm index", "helmchart.index", total=1
    ) as parsing:
        try:
            index_result = _safe_fetch(remote, index_url)
        except Exception as exc:
            if redact_url(index_url) != index_url:
                status = _http_status(exc)
                raise HelmChartError(
                    f"Index download failed for {redact_url(index_url)!r}: "
                    f"HTTP {status if status is not None else 'network error'}."
                ) from None
            raise
        with open(index_result.path, "rb") as index_file:
            all_entries = parse_repository_index(index_file)
        parsing.increment()
    entries = _filter_entries(all_entries, remote)
    auto_excluded_count = sum(
        entry.version in remote.auto_excluded_versions.get(entry.chart_name, {})
        for entry in all_entries
    )
    with task_progress(
        f"Selected {len(entries)} Helm chart versions", "helmchart.selected", total=1
    ) as selected:
        selected.increment()

    synced_content = []
    skipped_unavailable = []
    downloaded = 0
    reused = 0
    index_digest = _sha256_path(index_result.path)

    with ExitStack() as progress_stack:
        downloading = progress_stack.enter_context(
            task_progress(
                "Downloading Helm chart versions",
                "helmchart.download",
                total=len(entries),
            )
        )
        unavailable_progress = progress_stack.enter_context(
            task_progress("Skipped unavailable charts", "helmchart.unavailable")
        )
        checksum_progress = progress_stack.enter_context(
            task_progress("Skipped checksum mismatches", "helmchart.checksum_skip")
        )
        created_progress = progress_stack.enter_context(
            task_progress("Created Helm chart content", "helmchart.created")
        )
        reused_progress = progress_stack.enter_context(
            task_progress("Reused Helm chart content", "helmchart.reused")
        )
        auto_progress = progress_stack.enter_context(
            task_progress(
                "Automatically excluded chart versions", "helmchart.auto_excluded"
            )
        )
        auto_progress.done = auto_excluded_count
        auto_progress.save()
        base_version = repository.latest_version()
        with repository.new_version() as new_version:
            for entry in downloading.iter(entries):
                # Also skip repeated index entries excluded earlier in this sync.
                if entry.version in remote.auto_excluded_versions.get(
                    entry.chart_name, {}
                ):
                    continue
                chart_result, chart_url, filename, unavailable = (
                    _fetch_chart_from_candidates(remote, entry)
                )
                if unavailable:
                    skipped_unavailable.append(unavailable)
                    unavailable_progress.increment()
                    continue
                safe_url = redact_url(chart_url)
                chart_digest = _sha256_path(chart_result.path)
                try:
                    verify_sha256_digest(entry.digest, chart_digest, safe_url)
                except HelmChartError as exc:
                    message = (
                        f"Chart {entry.chart_name!r} version {entry.version!r}: {exc}"
                    )
                    if remote.checksum_mismatch_policy not in {"skip", "exclude"}:
                        raise HelmChartError(message) from exc
                    log.warning(
                        "Skipping Helm chart with checksum mismatch: %s", message
                    )
                    checksum_progress.increment()
                    if remote.checksum_mismatch_policy == "exclude":
                        _record_auto_exclusion(remote, entry, safe_url, chart_digest)
                        auto_progress.increment()
                    continue

                with open(chart_result.path, "rb") as chart_file:
                    uploaded = PulpTemporaryUploadedFile.from_file(chart_file)
                    result = create_helmchart_content_from_tgz(
                        uploaded, relative_path=filename
                    )

                if (
                    result.content.name != entry.chart_name
                    or result.content.version != entry.version
                ):
                    raise HelmChartError(
                        "Chart archive metadata does not match upstream index entry: "
                        f"index has {entry.chart_name!r} {entry.version!r}, archive has "
                        f"{result.content.name!r} {result.content.version!r}."
                    )

                if result.created:
                    downloaded += 1
                    created_progress.increment()
                else:
                    reused += 1
                    reused_progress.increment()
                synced_content.append(result.content.pk)

            added_content_ids = set(synced_content)
            if base_version and added_content_ids:
                added_content_ids.difference_update(
                    base_version.content.filter(pk__in=added_content_ids).values_list(
                        "pk", flat=True
                    )
                )
            if synced_content:
                new_version.add_content(
                    HelmChartContent.objects.filter(pk__in=synced_content)
                )

    latest_version = repository.latest_version()
    repository.last_sync_details = {
        "remote_pk": str(remote.pk),
        "url": redact_url(remote.url),
        "index_url": redact_url(index_url),
        "index_checksum": index_digest,
        "synced_at": timezone.now().isoformat(),
        "charts_seen": len(entries),
        "charts_available": len(all_entries),
        "charts_added_or_updated": len(added_content_ids),
        "charts_created": downloaded,
        "charts_reused": reused,
        "charts_skipped_unavailable": len(skipped_unavailable),
        "skipped_unavailable": skipped_unavailable,
        "sync_mode": "additive",
        "most_recent_version": latest_version.number,
    }
    repository.save()

    repo_version = RepositoryVersion.objects.filter(pk=latest_version.pk).first()
    if repo_version:
        return RepositoryVersionSerializer(
            instance=repo_version, context={"request": None}
        ).data
    return None


def _chart_filename(url, chart_name, version):
    filename = os.path.basename(unquote(urlparse(url).path))
    return filename or default_archive_filename(chart_name, version)


def _fetch_chart_from_candidates(remote, entry):
    """Try safe declared mirrors in order; a downloaded checksum is handled by the caller."""
    failures = []
    canonical_filename = None
    for candidate in entry.urls:
        safe_candidate = redact_url(candidate)
        try:
            chart_url = validate_chart_url(
                remote.url, candidate, remote.allowed_chart_hosts
            )
        except HelmChartError:
            failures.append(("policy", safe_candidate, None))
            log.warning(
                "Rejecting Helm chart URL by remote policy: name=%s version=%s url=%s",
                entry.chart_name,
                entry.version,
                safe_candidate,
            )
            continue

        if canonical_filename is None:
            canonical_filename = _chart_filename(
                chart_url, entry.chart_name, entry.version
            )
        safe_url = redact_url(chart_url)
        try:
            return _safe_fetch(remote, chart_url), chart_url, canonical_filename, None
        except (
            aiohttp.ClientError,
            TimeoutException,
            TimeoutError,
            HelmChartError,
        ) as exc:
            status = _http_status(exc)
            kind = "unavailable" if status in UNAVAILABLE_HTTP_STATUSES else "download"
            failures.append((kind, safe_url, status))
            log.warning(
                "Helm chart URL failed: name=%s version=%s url=%s status=%s",
                entry.chart_name,
                entry.version,
                safe_url,
                status if status is not None else "network error",
            )

    attempted = [failure for failure in failures if failure[0] != "policy"]
    if (
        remote.ignore_unavailable
        and attempted
        and all(failure[0] == "unavailable" for failure in attempted)
    ):
        _, url, status = attempted[-1]
        skipped = {
            "name": entry.chart_name,
            "version": entry.version,
            "url": url,
            "status": status,
        }
        log.warning(
            "Skipping unavailable Helm chart after %s URL(s): name=%s version=%s",
            len(entry.urls),
            entry.chart_name,
            entry.version,
        )
        return None, None, None, skipped

    summary = "; ".join(
        f"{kind} {url} (HTTP {status})" if status is not None else f"{kind} {url}"
        for kind, url, status in failures[:3]
    )
    if len(failures) > 3:
        summary += f"; {len(failures) - 3} more URL(s)"
    raise HelmChartError(
        f"All {len(entry.urls)} chart URLs failed for {entry.chart_name!r} "
        f"version {entry.version!r}: {summary}."
    )


def _safe_fetch(remote, url):
    """Follow only trusted redirects, using an anonymous pulpcore downloader off origin."""
    current = validate_chart_url(remote.url, url, remote.allowed_chart_hosts)
    visited = set()
    for hop in range(10):
        if current in visited:
            raise HelmChartError(f"Redirect loop at {redact_url(current)!r}.")
        visited.add(current)
        if _http_origin(urlsplit(current)) == _http_origin(urlsplit(remote.url)):
            downloader = remote.get_downloader(url=current)
        else:
            factory = getattr(remote, "_anonymous_chart_factory", None)
            if factory is None:
                anonymous = copy.copy(remote)
                for field in (
                    "username",
                    "password",
                    "headers",
                    "client_cert",
                    "client_key",
                    "proxy_url",
                    "proxy_username",
                    "proxy_password",
                ):
                    setattr(anonymous, field, None)
                factory = DownloaderFactory(anonymous)
                remote._anonymous_chart_factory = factory
            downloader = remote.get_downloader(url=current, download_factory=factory)
        try:
            result = downloader.fetch(
                extra_data={"request_kwargs": {"allow_redirects": False}}
            )
        except Exception as exc:
            if current != url or redact_url(current) != current:
                status = _http_status(exc)
                error = HelmChartError(
                    f"Download failed for {redact_url(current)!r}: "
                    f"HTTP {status if status is not None else 'network error'}."
                )
                error.status = status
                raise error from None
            raise
        location = getattr(result, "headers", {}).get("Location")
        if not location:
            return result
        if getattr(result, "path", None):
            os.unlink(result.path)
        try:
            next_url = validate_chart_url(
                remote.url, location, remote.allowed_chart_hosts, base_url=current
            )
        except HelmChartError as exc:
            raise HelmChartError(
                f"Unsafe redirect from {redact_url(current)!r}: {exc}"
            ) from None
        current = next_url
    raise HelmChartError(f"Too many redirects from {redact_url(url)!r}.")


def _filter_entries(entries, remote):
    return filter_repository_entries(
        entries,
        include_charts=remote.include_charts,
        exclude_charts=remote.exclude_charts,
        include_versions=remote.include_versions,
        exclude_versions=remote.exclude_versions,
        auto_excluded_versions=remote.auto_excluded_versions,
        latest_only=remote.latest_only,
    )


def _record_auto_exclusion(remote, entry, url, actual):
    """Merge under a row lock so concurrent syncs cannot overwrite each other's entries."""
    metadata = {
        "reason": "checksum_mismatch",
        "expected": entry.digest.removeprefix("sha256:"),
        "actual": actual,
        "url": url,
    }
    with transaction.atomic():
        locked = HelmChartRemote.objects.select_for_update().get(pk=remote.pk)
        exclusions = locked.auto_excluded_versions
        versions = exclusions.setdefault(entry.chart_name, {})
        previous = versions.get(entry.version, {})
        if any(previous.get(key) != value for key, value in metadata.items()):
            versions[entry.version] = {
                **metadata,
                "timestamp": timezone.now().isoformat(),
            }
            locked.save(update_fields=["auto_excluded_versions"])
        remote.auto_excluded_versions = exclusions


def retry_auto_exclusion(remote_pk, chart, version):
    """Remove only the requested exclusion from the latest locked remote state."""
    with transaction.atomic():
        remote = HelmChartRemote.objects.select_for_update().get(pk=remote_pk)
        exclusions = remote.auto_excluded_versions
        versions = exclusions.get(chart, {})
        if version not in versions:
            return
        del versions[version]
        if not versions:
            del exclusions[chart]
        remote.save(update_fields=["auto_excluded_versions"])


def _http_status(exc):
    status = getattr(exc, "status", None)
    if status is not None:
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status", None) or getattr(
            response, "status_code", None
        )
        if status is not None:
            return status
    return getattr(exc, "status_code", None)


def _sha256_path(path):
    with open(path, "rb") as file:
        return sha256_file(file)
