"""Database regressions for the release-blocker state mutations."""

import atexit
import asyncio
import io
import hashlib
import os
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests
import yaml
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.db.models.signals import post_migrate
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone
from django_guid import set_guid
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from pulpcore.app.models.role import Role
from pulpcore.app.models.status import AppStatus
from pulpcore.app.role_util import assign_role
from pulpcore.app.util import get_default_domain, get_prn
from pulpcore.app.contexts import with_task_context
from pulpcore.app import tasks as core_tasks
from pulpcore.plugin.models import (
    ContentArtifact,
    Artifact,
    ProgressReport,
    PublishedArtifact,
    Task,
    Upload,
    UploadChunk,
)
from pulpcore.tasking.tasks import (
    are_resources_available,
    dispatch,
    get_resources,
    get_task_function,
)
from pulpcore.plugin.files import PulpTemporaryUploadedFile
from pulp_file.app.models import FilePublication, FileRepository

from pulp_helmchart.app.content import create_helmchart_content_from_tgz
from pulp_helmchart.app.models import (
    HelmChartContent,
    HelmChartRemote,
    HelmChartRepository,
)
from pulp_helmchart.app.serializers import HelmChartContentSerializer
from pulp_helmchart.app.viewsets import (
    HelmChartContentViewSet,
    HelmChartDistributionViewSet,
    HelmChartPublicationViewSet,
    HelmChartRemoteViewSet,
)
from pulp_helmchart.app.upgrade import prevent_mixed_version_migration
from pulp_helmchart.app.tasks.publishing import publish, yield_index_entries_for_version
from pulp_helmchart.app.tasks.synchronizing import (
    _record_auto_exclusion,
    retry_auto_exclusion,
    synchronize,
)
from pulp_helmchart.app.tasks import synchronizing as sync_tasks
from pulp_helmchart import helm
from pulp_helmchart.helm import HelmChartError, RepositoryChartEntry, utc_timestamp


pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        not os.environ.get("PULP_SETTINGS"), reason="Requires isolated Pulp test DB"
    ),
]
_CHART_FILES = []


@pytest.fixture(scope="module", autouse=True)
def _pulp_post_migrate_test_compatibility(django_db_setup):
    """Pulp's signal handlers require `apps`, which Django flush does not provide."""
    from pulpcore.app.apps import (
        _populate_access_policies,
        _populate_artifact_serving_distribution,
        _populate_roles,
        _populate_system_id,
    )

    for config in apps.get_app_configs():
        post_migrate.disconnect(
            sender=config, dispatch_uid="populate_access_policies_identifier"
        )
        post_migrate.disconnect(sender=config, dispatch_uid="populate_roles_identifier")
    core = apps.get_app_config("core")
    post_migrate.disconnect(sender=core, dispatch_uid="populate_system_id_identifier")
    post_migrate.disconnect(
        sender=core, dispatch_uid="populate_artifact_serving_distribution_identifier"
    )
    yield
    for config in apps.get_app_configs():
        post_migrate.connect(
            _populate_access_policies,
            sender=config,
            dispatch_uid="populate_access_policies_identifier",
        )
        post_migrate.connect(
            _populate_roles, sender=config, dispatch_uid="populate_roles_identifier"
        )
    post_migrate.connect(
        _populate_system_id, sender=core, dispatch_uid="populate_system_id_identifier"
    )
    post_migrate.connect(
        _populate_artifact_serving_distribution,
        sender=core,
        dispatch_uid="populate_artifact_serving_distribution_identifier",
    )


@pytest.fixture(autouse=True)
def _reset_cached_default_domain():
    from pulpcore.app import util

    util.default_domain = None
    util.get_default_domain()


@pytest.fixture(autouse=True)
def _close_chart_files():
    yield
    for file in _CHART_FILES:
        path = file.file.name
        file.close()
        if os.path.exists(path):
            os.unlink(path)
    _CHART_FILES.clear()


@pytest.fixture
def close_pulp_downloaders(monkeypatch):
    from pulpcore.download.factory import DownloaderFactory

    factories = []
    original_init = DownloaderFactory.__init__

    def track_factory(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        factories.append(self)

    monkeypatch.setattr(DownloaderFactory, "__init__", track_factory)
    yield
    loop = asyncio.get_event_loop()
    for factory in factories:
        loop.run_until_complete(factory._session.close())
        atexit.unregister(factory._session_cleanup)


@lru_cache(maxsize=1)
def _chart_bytes():
    metadata = yaml.safe_dump(
        {"apiVersion": "v2", "name": "sample", "version": "1.0.0"}
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = metadata.encode()
        info = tarfile.TarInfo("sample/Chart.yaml")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _chart_file(filename):
    data = _chart_bytes()
    file = PulpTemporaryUploadedFile(filename, "application/gzip", len(data), "")
    file.file._closer.delete = False
    file.write(data)
    file.seek(0)
    for hasher in file.hashers.values():
        hasher.update(data)
    _CHART_FILES.append(file)
    return file


def _chart_file_from_bytes(filename, data):
    file = PulpTemporaryUploadedFile(filename, "application/gzip", len(data), "")
    file.file._closer.delete = False
    file.write(data)
    file.seek(0)
    for hasher in file.hashers.values():
        hasher.update(data)
    _CHART_FILES.append(file)
    return file


def _archive_bytes(paths, metadata=None):
    if metadata is None:
        metadata = {"apiVersion": "v2", "name": "sample", "version": "1.0.0"}
    payload = (
        metadata if isinstance(metadata, bytes) else yaml.safe_dump(metadata).encode()
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in paths:
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.mark.parametrize("version", ["1.0.0-rc.1", "v26.3.3"])
def test_helm_valid_prerelease_and_vprefix_create_content(version):
    data = _archive_bytes(
        ["sample/Chart.yaml"],
        {"apiVersion": "v2", "name": "sample", "version": version},
    )
    result = create_helmchart_content_from_tgz(
        _chart_file_from_bytes(f"sample-{version}.tgz", data)
    )
    assert result.content.version == version
    assert ContentArtifact.objects.filter(content=result.content).count() == 1


@pytest.mark.parametrize(
    "paths, metadata",
    [
        (
            ["sample/Chart.yaml"],
            {"apiVersion": "v2", "name": "sample", "version": "banana"},
        ),
        (["sample/nested/Chart.yaml"], None),
        (["../sample/Chart.yaml"], None),
        (["/sample/Chart.yaml"], None),
        (["sample/Chart.yaml", "other/Chart.yaml"], None),
        (["sample/Chart.yaml"], b"name: [unterminated"),
    ],
)
def test_invalid_helm_archive_never_creates_content(paths, metadata):
    data = _archive_bytes(paths, metadata)
    before = (HelmChartContent.objects.count(), ContentArtifact.objects.count())
    with pytest.raises(ValidationError):
        create_helmchart_content_from_tgz(_chart_file_from_bytes("invalid.tgz", data))
    assert (HelmChartContent.objects.count(), ContentArtifact.objects.count()) == before


@pytest.mark.parametrize("limit", ["metadata", "members"])
def test_archive_limits_reject_before_artifact_or_content(monkeypatch, limit):
    if limit == "metadata":
        monkeypatch.setattr(helm, "MAX_CHART_METADATA_SIZE", 64)
        data = _archive_bytes(
            ["sample/Chart.yaml"],
            {"name": "sample", "version": "1.0.0", "description": "x" * 100},
        )
    else:
        monkeypatch.setattr(helm, "MAX_CHART_ARCHIVE_MEMBERS", 1)
        data = _archive_bytes(["sample/Chart.yaml", "sample/values.yaml"])
    before = (Artifact.objects.count(), HelmChartContent.objects.count())
    with pytest.raises(ValidationError):
        create_helmchart_content_from_tgz(_chart_file_from_bytes("oversize.tgz", data))
    assert (Artifact.objects.count(), HelmChartContent.objects.count()) == before


def test_direct_upload_metadata_limit_rejects_before_artifact_or_task(monkeypatch):
    monkeypatch.setattr(helm, "MAX_CHART_METADATA_SIZE", 64)
    data = _archive_bytes(
        ["sample/Chart.yaml"],
        {"name": "sample", "version": "1.0.0", "description": "x" * 100},
    )
    user = get_user_model().objects.create_superuser(username=f"oversize-{uuid4()}")
    request = APIRequestFactory().post(
        "/pulp/api/v3/content/helmchart/chart/upload/",
        {"file": SimpleUploadedFile("oversize.tgz", data)},
        format="multipart",
    )
    request.pulp_domain = get_default_domain()
    force_authenticate(request, user=user)
    before = (
        Artifact.objects.count(),
        HelmChartContent.objects.count(),
        Task.objects.count(),
    )
    response = HelmChartContentViewSet.as_view({"post": "upload"})(request)
    assert response.status_code == 400
    assert (
        Artifact.objects.count(),
        HelmChartContent.objects.count(),
        Task.objects.count(),
    ) == before


def test_chunk_upload_metadata_limit_rejects_before_artifact(monkeypatch):
    monkeypatch.setattr(helm, "MAX_CHART_METADATA_SIZE", 64)
    data = _archive_bytes(
        ["sample/Chart.yaml"],
        {"name": "sample", "version": "1.0.0", "description": "x" * 100},
    )
    upload = Upload.objects.create(size=len(data))
    upload.append(ContentFile(data), 0)
    before = (Artifact.objects.count(), HelmChartContent.objects.count())
    serializer = HelmChartContentSerializer(
        data={"upload": f"/pulp/api/v3/uploads/{upload.pk}/"},
        context={"request": None},
    )
    assert not serializer.is_valid()
    assert (Artifact.objects.count(), HelmChartContent.objects.count()) == before


def _repository():
    return HelmChartRepository.objects.create(name=f"helm-{uuid4()}")


def _post_as_admin(viewset, path, payload):
    user = get_user_model().objects.create_superuser(username=f"helm-admin-{uuid4()}")
    set_guid(str(uuid4()))
    request = APIRequestFactory().post(path, payload, format="json")
    request.pulp_domain = get_default_domain()
    force_authenticate(request, user=user)
    return viewset.as_view({"post": "create"})(request)


def test_publication_rejects_nested_index_before_dispatch():
    repository = _repository()
    version = repository.latest_version()
    path = "/pulp/api/v3/publications/helmchart/helmchart/"
    payload = {
        "repository_version": f"/pulp/api/v3/repositories/helmchart/helmchart/{repository.pk}/versions/{version.number}/",
        "index": "nested/index.yaml",
    }
    before = Task.objects.count()
    response = _post_as_admin(HelmChartPublicationViewSet, path, payload)
    assert response.status_code == 400
    assert "index" in response.data
    assert Task.objects.count() == before
    payload["index"] = "index.yaml"
    response = _post_as_admin(HelmChartPublicationViewSet, path, payload)
    assert response.status_code == 202
    assert Task.objects.count() == before + 1


def test_helm_endpoints_reject_file_resources_before_dispatch():
    file_repository = FileRepository.objects.create(name=f"file-{uuid4()}")
    file_version = file_repository.latest_version()
    file_publication = FilePublication.objects.create(
        repository_version=file_version, pass_through=True, complete=True
    )

    publication_path = "/pulp/api/v3/publications/helmchart/helmchart/"
    distribution_path = "/pulp/api/v3/distributions/helmchart/helmchart/"
    foreign_repository = f"/pulp/api/v3/repositories/file/file/{file_repository.pk}/"
    foreign_version = f"{foreign_repository}versions/{file_version.number}/"
    foreign_publication = f"/pulp/api/v3/publications/file/file/{file_publication.pk}/"
    before = Task.objects.count()
    for payload in (
        {"repository": foreign_repository},
        {"repository_version": foreign_version},
    ):
        response = _post_as_admin(
            HelmChartPublicationViewSet, publication_path, payload
        )
        assert response.status_code in {400, 404}
        assert Task.objects.count() == before

    for association, href in (
        ("repository", foreign_repository),
        ("repository_version", foreign_version),
        ("publication", foreign_publication),
    ):
        payload = {
            "name": f"foreign-{uuid4()}",
            "base_path": f"foreign-{uuid4()}",
            association: href,
        }
        response = _post_as_admin(
            HelmChartDistributionViewSet, distribution_path, payload
        )
        assert response.status_code in {400, 404}
        assert Task.objects.count() == before


def test_sync_uses_second_url_with_real_http_and_database(close_pulp_downloaders):
    data = _chart_bytes()
    digest = hashlib.sha256(data).hexdigest()
    requested = []
    first_available = {"value": False}
    index = yaml.safe_dump(
        {
            "apiVersion": "v1",
            "entries": {
                "sample": [
                    {
                        "name": "sample",
                        "version": "1.0.0",
                        "digest": digest,
                        "urls": ["missing.tgz", "sample-1.0.0.tgz"],
                    }
                ]
            },
        }
    ).encode()

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            if self.path == "/index.yaml":
                status, payload = 200, index
            elif self.path == "/missing.tgz" and first_available["value"]:
                status, payload = 200, data
            elif self.path == "/sample-1.0.0.tgz":
                status, payload = 200, data
            else:
                status, payload = 404, b"missing"
            self.send_response(status)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        remote = HelmChartRemote.objects.create(
            name=f"fallback-{uuid4()}",
            url=f"http://127.0.0.1:{server.server_port}/",
            policy="immediate",
            ignore_unavailable=False,
        )
        repository = _repository()
        assert repository.latest_version().number == 0
        synchronize(remote.pk, repository.pk)
        repository.refresh_from_db()
        assert repository.latest_version().number == 1
        assert repository.last_sync_details["charts_added_or_updated"] == 1
        assert repository.last_sync_details["charts_created"] == 1
        assert repository.last_sync_details["charts_reused"] == 0
        assert requested == ["/index.yaml", "/missing.tgz", "/sample-1.0.0.tgz"]
        content = [unit.cast() for unit in repository.latest_version().content.all()]
        assert len(content) == 1
        assert content[0].name == "sample"
        assert content[0].filename == "missing.tgz"
        first_available["value"] = True
        requested.clear()
        synchronize(remote.pk, repository.pk)
        repository.refresh_from_db()
        assert repository.latest_version().number == 1
        assert repository.last_sync_details["charts_added_or_updated"] == 0
        assert repository.last_sync_details["charts_created"] == 0
        assert repository.last_sync_details["charts_reused"] == 1
        assert requested == ["/index.yaml", "/missing.tgz"]
        assert HelmChartContent.objects.count() == 1
        assert list(
            ContentArtifact.objects.filter(content=content[0]).values_list(
                "relative_path", flat=True
            )
        ) == ["missing.tgz"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("direct_index", [False, True])
def test_query_authenticated_index_keeps_token_off_relative_chart_urls(
    close_pulp_downloaders, direct_index
):
    chart = _chart_bytes()
    requested = []
    index = yaml.safe_dump(
        {
            "apiVersion": "v1",
            "entries": {
                "sample": [
                    {
                        "name": "sample",
                        "version": "1.0.0",
                        "urls": ["sample-1.0.0.tgz"],
                        "digest": hashlib.sha256(chart).hexdigest(),
                    }
                ]
            },
        }
    ).encode()

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            if self.path == "/charts/index.yaml?token=index-secret":
                status, body = 200, index
            elif self.path == "/charts/sample-1.0.0.tgz":
                status, body = 200, chart
            else:
                status, body = 404, b"not found"
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        suffix = (
            "index.yaml?token=index-secret" if direct_index else "?token=index-secret"
        )
        remote = HelmChartRemote.objects.create(
            name=f"query-{uuid4()}",
            url=f"http://127.0.0.1:{server.server_port}/charts/{suffix}",
        )
        repository = _repository()
        synchronize(remote.pk, repository.pk)
        assert requested == [
            "/charts/index.yaml?token=index-secret",
            "/charts/sample-1.0.0.tgz",
        ]
        repository.refresh_from_db()
        assert "index-secret" not in str(repository.last_sync_details)
        assert repository.latest_version().content.count() == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_legacy_unsupported_remote_policy_fails_before_repository_change():
    remote = HelmChartRemote.objects.create(
        name=f"legacy-policy-{uuid4()}",
        url="https://example.com/charts/",
        policy="on_demand",
    )
    repository = _repository()
    with pytest.raises(HelmChartError, match="unsupported"):
        synchronize(remote.pk, repository.pk)
    assert repository.latest_version().number == 0


def test_reused_chart_path_cannot_mutate_published_or_other_repository():
    first = create_helmchart_content_from_tgz(_chart_file("sample-1.0.0.tgz"))
    first_repository, second_repository = _repository(), _repository()
    for repository in (first_repository, second_repository):
        with repository.new_version() as version:
            version.add_content(HelmChartContent.objects.filter(pk=first.content.pk))

    publish(first_repository.latest_version().pk, record_created_resource=False)
    publication_paths = list(
        PublishedArtifact.objects.filter(
            publication__repository_version=first_repository.latest_version()
        ).values_list("relative_path", flat=True)
    )
    assert publication_paths == ["index.yaml"]
    index_before = PublishedArtifact.objects.get(
        publication__repository_version=first_repository.latest_version(),
        relative_path="index.yaml",
    ).content_artifact.artifact.sha256

    reused = create_helmchart_content_from_tgz(_chart_file("sample-1.0.0.tgz"))
    assert reused.content.pk == first.content.pk
    assert not reused.created

    with pytest.raises(ValidationError, match="different archive filename"):
        create_helmchart_content_from_tgz(_chart_file("renamed.tgz"))

    assert list(
        ContentArtifact.objects.filter(content=first.content).values_list(
            "relative_path", flat=True
        )
    ) == ["sample-1.0.0.tgz"]
    assert (
        list(
            PublishedArtifact.objects.filter(
                publication__repository_version=first_repository.latest_version()
            ).values_list("relative_path", flat=True)
        )
        == publication_paths
    )
    assert (
        PublishedArtifact.objects.get(
            publication__repository_version=first_repository.latest_version(),
            relative_path="index.yaml",
        ).content_artifact.artifact.sha256
        == index_before
    )
    for repository in (first_repository, second_repository):
        entries = list(
            yield_index_entries_for_version(
                repository.latest_version(), utc_timestamp()
            )
        )
        assert len(entries) == 1
        assert entries[0]["urls"] == ["sample-1.0.0.tgz"]


def test_existing_ambiguous_content_fails_publication():
    result = create_helmchart_content_from_tgz(_chart_file("sample-1.0.0.tgz"))
    ContentArtifact.objects.create(
        content=result.content, artifact=result.artifact, relative_path="second.tgz"
    )
    repository = _repository()
    with repository.new_version() as version:
        version.add_content(HelmChartContent.objects.filter(pk=result.content.pk))
    with pytest.raises(ValueError, match="ambiguous archive paths"):
        publish(repository.latest_version().pk, record_created_resource=False)


def test_upload_requires_repo_permission_and_dispatches_reserved_task():
    content = create_helmchart_content_from_tgz(_chart_file("sample-1.0.0.tgz"))
    repository = _repository()
    user = get_user_model().objects.create_user(username=f"uploader-{uuid4()}")
    user.user_permissions.add(Permission.objects.get(codename="upload_helmchart"))
    endpoint = "/pulp/api/v3/content/helmchart/chart/upload/"
    payload = {
        "artifact": f"/pulp/api/v3/artifacts/{content.artifact.pk}/",
        "repository": f"/pulp/api/v3/repositories/helmchart/helmchart/{repository.pk}/",
        "relative_path": "sample-1.0.0.tgz",
    }
    view = HelmChartContentViewSet.as_view({"post": "upload"})

    def request():
        set_guid(str(uuid4()))
        result = APIRequestFactory().post(endpoint, payload, format="json")
        result.pulp_domain = get_default_domain()
        force_authenticate(result, user=user)
        return view(result)

    denied = request()
    assert denied.status_code == 403
    assert repository.latest_version().number == 0
    assert Task.objects.count() == 0

    user.user_permissions.add(
        Permission.objects.get(codename="modify_helmchartrepository"),
        Permission.objects.get(codename="view_helmchartrepository"),
    )
    user = get_user_model().objects.get(pk=user.pk)
    allowed = request()
    assert allowed.status_code == 202
    task = Task.objects.get()
    assert get_prn(repository) in task.reserved_resources_record
    assert repository.latest_version().number == 0

    modify_task = dispatch(
        core_tasks.repository.add_and_remove,
        exclusive_resources=[repository],
        kwargs={
            "repository_pk": str(repository.pk),
            "base_version_pk": None,
            "add_content_units": [],
            "remove_content_units": [],
        },
    )
    colliding, _ = get_resources([repository], [], immediate=True)
    assert get_prn(repository) in modify_task.reserved_resources_record
    assert not are_resources_available(colliding, modify_task)

    with with_task_context(task):
        get_task_function(task)()
    assert repository.latest_version().number == 1
    assert repository.latest_version().content.filter(pk=content.content.pk).exists()
    Task.objects.filter(pk=task.pk).update(state="completed")
    assert are_resources_available(colliding, modify_task)

    second = request()
    assert second.status_code == 202
    reservations = list(
        Task.objects.values_list("reserved_resources_record", flat=True)
    )
    assert all(get_prn(repository) in resources for resources in reservations)

    payload["relative_path"] = "renamed.tgz"
    assert request().status_code == 202
    renamed_task = Task.objects.order_by("-pulp_created").first()
    with (
        with_task_context(renamed_task),
        pytest.raises(ValidationError, match="different archive filename"),
    ):
        get_task_function(renamed_task)()
    assert list(
        ContentArtifact.objects.filter(content=content.content).values_list(
            "relative_path", flat=True
        )
    ) == ["sample-1.0.0.tgz"]


def test_foreign_upload_requires_core_change_upload_over_http(live_server):
    chart = _chart_bytes()
    # Test DB flushing omits Pulp's post-migrate role population.
    upload_owner, _ = Role.objects.get_or_create(name="core.upload_owner")
    upload_owner.permissions.add(
        *Permission.objects.filter(
            codename__in=[
                "view_upload",
                "change_upload",
                "delete_upload",
                "manage_roles_upload",
            ]
        )
    )
    task_owner, _ = Role.objects.get_or_create(name="core.task_owner")
    task_owner.permissions.add(
        *Permission.objects.filter(
            codename__in=[
                "view_task",
                "change_task",
                "delete_task",
                "manage_roles_task",
            ]
        )
    )
    task_dispatcher, _ = Role.objects.get_or_create(name="core.task_user_dispatcher")
    task_dispatcher.permissions.add(Permission.objects.get(codename="add_task"))
    owner = get_user_model().objects.create_superuser(
        username=f"upload-owner-{uuid4()}", password="owner-test-password"
    )
    other = get_user_model().objects.create_user(
        username=f"helm-uploader-{uuid4()}", password="other-test-password"
    )
    other.user_permissions.add(Permission.objects.get(codename="upload_helmchart"))
    owner_auth = (owner.username, "owner-test-password")
    other_auth = (other.username, "other-test-password")
    upload_url = f"{live_server.url}/pulp/api/v3/uploads/"
    response = requests.post(
        upload_url, json={"size": len(chart)}, auth=owner_auth, timeout=10
    )
    assert response.status_code == 201, response.text
    upload_href = response.json()["pulp_href"]
    response = requests.put(
        f"{live_server.url}{upload_href}",
        files={"file": ("sample.tgz", chart)},
        headers={"Content-Range": f"bytes 0-{len(chart) - 1}/{len(chart)}"},
        auth=owner_auth,
        timeout=10,
    )
    assert response.status_code == 200, response.text

    upload = Upload.objects.get(pk=upload_href.rstrip("/").split("/")[-1])
    repository = _repository()
    before = (Task.objects.count(), HelmChartContent.objects.count())
    endpoint = f"{live_server.url}/pulp/api/v3/content/helmchart/chart/upload/"
    payload = {"upload": upload_href, "relative_path": "sample-1.0.0.tgz"}
    response = requests.post(endpoint, json=payload, auth=other_auth, timeout=10)
    assert response.status_code == 403, response.text
    assert (Task.objects.count(), HelmChartContent.objects.count()) == before
    assert repository.latest_version().number == 0
    assert Upload.objects.filter(pk=upload.pk).exists()
    assert (
        b"".join(
            chunk.file.read()
            for chunk in UploadChunk.objects.filter(upload=upload).order_by("offset")
        )
        == chart
    )
    assert (
        requests.get(
            f"{live_server.url}{upload_href}", auth=owner_auth, timeout=10
        ).status_code
        == 200
    )

    change_role = Role.objects.create(name=f"upload-change-only-{uuid4()}")
    change_role.permissions.add(Permission.objects.get(codename="change_upload"))
    assign_role(change_role.name, other, obj=upload)
    response = requests.post(endpoint, json=payload, auth=other_auth, timeout=10)
    assert response.status_code == 202, response.text
    assert Task.objects.count() == before[0] + 1
    task = Task.objects.order_by("-pulp_created").first()
    with with_task_context(task):
        get_task_function(task)()
    assert HelmChartContent.objects.count() == before[1] + 1
    assert not Upload.objects.filter(pk=upload.pk).exists()


def _remote_with_exclusions():
    return HelmChartRemote.objects.create(
        name=f"remote-{uuid4()}",
        url="https://charts.example/",
        auto_excluded_versions={
            "sample": {
                "1.0.0": {"reason": "checksum_mismatch"},
                "0.9.0": {"reason": "checksum_mismatch"},
            }
        },
    )


def test_retry_auto_exclusion_removes_only_one_version_and_is_idempotent():
    remote = _remote_with_exclusions()
    retry_auto_exclusion(remote.pk, "sample", "1.0.0")
    retry_auto_exclusion(remote.pk, "sample", "1.0.0")
    remote.refresh_from_db()
    assert set(remote.auto_excluded_versions["sample"]) == {"0.9.0"}


def test_retry_action_requires_modify_permission_and_reserves_remote():
    remote = _remote_with_exclusions()
    user = get_user_model().objects.create_user(username=f"remote-editor-{uuid4()}")
    user.user_permissions.add(Permission.objects.get(codename="view_helmchartremote"))
    assert user.has_perm("helmchart.view_helmchartremote")
    view_role = Role.objects.create(name=f"test-remote-view-{uuid4()}")
    view_role.permissions.add(Permission.objects.get(codename="view_helmchartremote"))
    assign_role(view_role.name, user, obj=remote)
    endpoint = (
        f"/pulp/api/v3/remotes/helmchart/helmchart/{remote.pk}/retry_auto_exclusion/"
    )
    view = HelmChartRemoteViewSet.as_view({"post": "retry_auto_exclusion"})

    def request():
        set_guid(str(uuid4()))
        result = APIRequestFactory().post(
            endpoint, {"chart": "sample", "version": "1.0.0"}, format="json"
        )
        result.pulp_domain = get_default_domain()
        force_authenticate(result, user=user)
        return view(result, pk=str(remote.pk))

    denied = request()
    assert denied.status_code == 403, denied.data
    remote.refresh_from_db()
    assert "1.0.0" in remote.auto_excluded_versions["sample"]

    user.user_permissions.add(Permission.objects.get(codename="change_helmchartremote"))
    user = get_user_model().objects.get(pk=user.pk)
    allowed = request()
    assert allowed.status_code == 202, allowed.data
    task = Task.objects.get()
    assert get_prn(remote) in task.reserved_resources_record
    with with_task_context(task):
        get_task_function(task)()
    remote.refresh_from_db()
    assert set(remote.auto_excluded_versions["sample"]) == {"0.9.0"}


@pytest.mark.parametrize("add_first", [True, False])
def test_concurrent_add_and_targeted_remove_preserves_new_diagnosis(add_first):
    remote = _remote_with_exclusions()
    entry = RepositoryChartEntry("sample", "2.0.0", ["sample.tgz"], "a" * 64, {})

    def add():
        close_old_connections()
        _record_auto_exclusion(
            remote, entry, "https://charts.example/sample.tgz", "b" * 64
        )
        close_old_connections()

    def remove():
        close_old_connections()
        retry_auto_exclusion(remote.pk, "sample", "1.0.0")
        close_old_connections()

    operations = (add, remove) if add_first else (remove, add)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(operation) for operation in operations]
        for future in futures:
            future.result()

    remote.refresh_from_db()
    assert set(remote.auto_excluded_versions["sample"]) == {"0.9.0", "2.0.0"}


def test_pre_0006_migration_guard_blocks_live_pulp_processes(monkeypatch):
    applied = MigrationRecorder.applied_migrations

    def before_0006(self):
        return {
            key: value
            for key, value in applied(self).items()
            if key != ("helmchart", "0006_remote_sync_policies")
        }

    monkeypatch.setattr(MigrationRecorder, "applied_migrations", before_0006)
    status = AppStatus(
        app_type="worker",
        name=f"test-worker-{uuid4()}",
        versions={},
        ttl=timedelta(minutes=5),
    )
    AppStatus.objects.bulk_create([status])
    config = apps.get_app_config("helmchart")
    with pytest.raises(RuntimeError, match="Stop all Pulp"):
        prevent_mixed_version_migration(config, "default")

    AppStatus.objects.filter(pk=status.pk).update(
        last_heartbeat=timezone.now() - timedelta(minutes=10)
    )
    prevent_mixed_version_migration(config, "default")


@pytest.mark.parametrize("failure", [None, "invalid", "save"])
def test_chunked_upload_cleans_assembly_and_consumed_upload(
    tmp_path, monkeypatch, failure
):
    data = b"not a chart" if failure == "invalid" else _chart_bytes()
    upload = Upload.objects.create(size=len(data))
    upload_pk = upload.pk
    upload.append(ContentFile(data), 0)
    serializer = HelmChartContentSerializer(
        data={
            "upload": f"/pulp/api/v3/uploads/{upload_pk}/",
            "relative_path": "sample-1.0.0.tgz",
        },
        context={"request": None},
    )
    if failure == "save":
        monkeypatch.setattr(
            serializer,
            "retrieve",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("save failed")),
        )
    with override_settings(FILE_UPLOAD_TEMP_DIR=str(tmp_path)):
        if failure == "invalid":
            assert not serializer.is_valid()
        else:
            assert serializer.is_valid(), serializer.errors
            if failure == "save":
                with pytest.raises(RuntimeError, match="save failed"):
                    serializer.save()
            else:
                assert serializer.save().name == "sample"
    assert not Upload.objects.filter(pk=upload_pk).exists()
    assert list(tmp_path.iterdir()) == []


def test_sync_and_publication_report_progress_with_real_pulp_task(
    tmp_path, monkeypatch
):
    from pulp_helmchart.app.tasks.publishing import publish

    chart_bytes = _chart_bytes()
    chart_path = tmp_path / "sample-1.0.0.tgz"
    chart_path.write_bytes(chart_bytes)
    index_path = tmp_path / "index.yaml"
    index_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "entries": {
                    "sample": [
                        {
                            "version": "1.0.0",
                            "urls": ["sample-1.0.0.tgz"],
                            "digest": hashlib.sha256(chart_bytes).hexdigest(),
                        }
                    ]
                },
            }
        )
    )
    monkeypatch.setattr(
        sync_tasks,
        "_safe_fetch",
        lambda _remote, url: SimpleNamespace(
            path=str(index_path if url.endswith("index.yaml") else chart_path)
        ),
    )
    remote = HelmChartRemote.objects.create(
        name=f"progress-{uuid4()}", url="https://example.test/"
    )
    repository = _repository()
    task = Task.objects.create(
        state="running", name="helmchart progress test", logging_cid="test"
    )
    with with_task_context(task):
        synchronize(remote.pk, repository.pk)
        publish(repository.latest_version().pk)
        chart_path.write_bytes(chart_bytes)
        synchronize(remote.pk, repository.pk)
        index_path.write_text(yaml.safe_dump({"apiVersion": "v1", "entries": {}}))
        synchronize(remote.pk, repository.pk)

    reports = {
        report.code: report for report in ProgressReport.objects.filter(task=task)
    }
    assert reports["helmchart.index"].done == 1
    assert reports["helmchart.selected"].done == 1
    assert reports["helmchart.publish_index"].done == 1
    assert reports["helmchart.publish_index"].state == "completed"
    downloads = list(
        ProgressReport.objects.filter(task=task, code="helmchart.download")
    )
    assert [report.done for report in downloads] == [1, 1, 0]
    assert ProgressReport.objects.filter(
        task=task, code="helmchart.created", done=1
    ).exists()
    assert ProgressReport.objects.filter(
        task=task, code="helmchart.reused", done=1
    ).exists()
    assert repository.latest_version().content.count() == 1


def test_state_only_0008_upgrades_existing_database_without_schema_change():
    executor = MigrationExecutor(connection)
    assert (
        "helmchart",
        "0008_normalize_inherited_field_state",
    ) in executor.loader.applied_migrations
    with connection.schema_editor(collect_sql=True) as editor:
        migration = executor.loader.get_migration(
            "helmchart", "0008_normalize_inherited_field_state"
        )
        old_state = executor.loader.project_state(
            ("helmchart", "0007_allowed_chart_hosts")
        )
        new_state = old_state.clone()
        migration.apply(new_state, editor, collect_sql=True)
        assert not any(
            statement.lstrip()
            .upper()
            .startswith(("ALTER ", "CREATE ", "DROP ", "UPDATE "))
            for statement in editor.collected_sql
        )

    executor.migrate([("helmchart", "0007_allowed_chart_hosts")])
    assert HelmChartContent.objects.exists() is False
    MigrationExecutor(connection).migrate(
        [("helmchart", "0008_normalize_inherited_field_state")]
    )
    assert ("helmchart", "0008_normalize_inherited_field_state") in MigrationRecorder(
        connection
    ).applied_migrations()
