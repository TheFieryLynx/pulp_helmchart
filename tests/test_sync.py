import asyncio
import atexit
import copy
import hashlib
import threading
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import aiohttp
import pytest
import yaml

from pulpcore.download.factory import DownloaderFactory
from pulpcore.exceptions import TimeoutException
from pulpcore.plugin.models import Remote
from pulpcore.plugin.serializers import RemoteSerializer
from rest_framework.exceptions import ValidationError

from pulp_helmchart.app.models import HelmChartRemote
from pulp_helmchart.app.serializers import HelmChartRemoteSerializer
from pulp_helmchart.app.tasks import synchronizing as sync
from pulp_helmchart.helm import HelmChartError, RepositoryChartEntry


@pytest.fixture(autouse=True)
def serializer_domain(monkeypatch):
    # These unit tests do not need a database-backed request domain.
    monkeypatch.setattr(
        "pulpcore.app.serializers.base.get_domain", lambda: SimpleNamespace(pk=uuid4())
    )


@pytest.fixture
def remote():
    return HelmChartRemote(
        pk=uuid4(),
        pulp_domain_id=uuid4(),
        name="test",
        url="https://example.test/",
        max_retries=8,
    )


def test_max_retries_is_inherited_and_writable():
    serializer = HelmChartRemoteSerializer(data={"max_retries": 8}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["max_retries"] == 8
    assert "max_retries" in RemoteSerializer.Meta.fields
    assert HelmChartRemote._meta.get_field("max_retries").model is Remote
    assert "max_retries" not in {
        field.name for field in HelmChartRemote._meta.local_fields
    }


@pytest.mark.parametrize("allow_destination", [False, True])
@pytest.mark.parametrize("auth_mode", ["basic", "header"])
def test_cross_host_redirect_does_not_forward_remote_credentials(
    remote, tmp_path, monkeypatch, allow_destination, auth_mode
):
    monkeypatch.setattr(
        "pulpcore.download.base.settings.WORKING_DIRECTORY", str(tmp_path)
    )
    received = []
    source_received = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"chart")

        def log_message(self, *_args):
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            source_received.append(dict(self.headers))
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://localhost:{destination.server_port}/chart.tgz?token=hidden#fragment",
            )
            self.end_headers()

        def log_message(self, *_args):
            pass

    source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (source, destination)
    ]
    for thread in threads:
        thread.start()
    try:
        remote.url = f"http://127.0.0.1:{source.server_port}/"
        if auth_mode == "basic":
            remote.username = "reader"
            remote.password = "secret"
        remote.headers = [{"Cookie": "session=private", "X-Token": "private"}]
        if auth_mode == "header":
            remote.headers[0]["Authorization"] = "Bearer private"
        remote.allowed_chart_hosts = ["localhost"] if allow_destination else []
        if allow_destination:
            result = sync._safe_fetch(remote, remote.url + "chart.tgz")
            assert open(result.path, "rb").read() == b"chart"
        else:
            with pytest.raises(HelmChartError, match="redirect") as error:
                sync._safe_fetch(remote, remote.url + "chart.tgz")
            assert "hidden" not in str(error.value)
        assert source_received and "Authorization" in source_received[0]
        if allow_destination:
            assert len(received) == 1
            for header in ("Authorization", "Cookie", "X-Token"):
                assert header not in received[0]
        else:
            assert received == []
    finally:
        factory = remote.download_factory
        asyncio.get_event_loop().run_until_complete(factory._session.close())
        atexit.unregister(factory._session_cleanup)
        anonymous = getattr(remote, "_anonymous_chart_factory", None)
        if anonymous:
            asyncio.get_event_loop().run_until_complete(anonymous._session.close())
            atexit.unregister(anonymous._session_cleanup)
        for server in (source, destination):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_same_origin_redirect_retains_remote_credentials(remote, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pulpcore.download.base.settings.WORKING_DIRECTORY", str(tmp_path)
    )
    received = []

    class SameOrigin(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, dict(self.headers)))
            if self.path == "/path/start":
                self.send_response(302)
                self.send_header("Location", "archive")
            else:
                self.send_response(200)
            self.end_headers()
            if self.path == "/path/archive":
                self.wfile.write(b"chart")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SameOrigin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        remote.url = f"http://127.0.0.1:{server.server_port}/"
        remote.username = "reader"
        remote.password = "secret"
        result = sync._safe_fetch(remote, remote.url + "path/start")
        assert open(result.path, "rb").read() == b"chart"
        assert [path for path, _ in received] == ["/path/start", "/path/archive"]
        assert all("Authorization" in headers for _, headers in received)
    finally:
        factory = remote.download_factory
        asyncio.get_event_loop().run_until_complete(factory._session.close())
        atexit.unregister(factory._session_cleanup)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_checksum_diagnostics_strip_url_query_secret(sync_context, caplog):
    ctx = sync_context
    ctx.remote.checksum_mismatch_policy = "exclude"
    document = yaml.safe_load(ctx.index.read_text())
    document["entries"]["alertmanager"][0]["urls"] = ["bad.tgz?token=private"]
    ctx.index.write_text(yaml.safe_dump(document))
    ctx.downloads["https://example.test/bad.tgz?token=private"] = ctx.downloads[
        "https://example.test/bad.tgz"
    ]

    sync.synchronize(ctx.remote.pk, uuid4())

    diagnosis = ctx.locked.auto_excluded_versions["alertmanager"]["1.18.0"]
    assert diagnosis["url"] == "https://example.test/bad.tgz"
    assert "private" not in caplog.text


def test_allowed_chart_hosts_serializer_validation():
    serializer = HelmChartRemoteSerializer(
        data={
            "allowed_chart_hosts": [
                "GitHub.COM",
                "github.com",
                "release-assets.githubusercontent.com",
            ]
        },
        partial=True,
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["allowed_chart_hosts"] == [
        "github.com",
        "release-assets.githubusercontent.com",
    ]
    for bad in (None, "github.com", [""], ["https://github.com"], ["github.com/path"]):
        serializer = HelmChartRemoteSerializer(
            data={"allowed_chart_hosts": bad}, partial=True
        )
        assert not serializer.is_valid()


@pytest.mark.parametrize("field", ["include_versions", "exclude_versions"])
def test_version_mapping_validation(field):
    serializer = HelmChartRemoteSerializer()
    mapping = {"alertmanager": ["1.18.0"], "*": ["1.17.0"]}
    assert serializer.fields[field].run_validation(mapping) == mapping
    for invalid in (
        ["1.18.0"],
        {"alertmanager": "1.18.0"},
        {"alertmanager": {"1.18.0": {}}},
    ):
        with pytest.raises(ValidationError):
            serializer.fields[field].run_validation(invalid)


def test_checksum_policy_validation_and_defaults(remote):
    field = HelmChartRemoteSerializer().fields["checksum_mismatch_policy"]
    assert field.get_default() == remote.checksum_mismatch_policy == "fail"
    for value in ("fail", "skip", "exclude"):
        assert field.run_validation(value) == value
    with pytest.raises(ValidationError):
        field.run_validation("accept")
    assert remote.auto_excluded_versions == {}


def test_helm_remote_only_accepts_immediate_policy(sync_context):
    for value in ("on_demand", "streamed"):
        serializer = HelmChartRemoteSerializer(data={"policy": value}, partial=True)
        assert not serializer.is_valid()
        sync_context.remote.policy = value
        with pytest.raises(HelmChartError, match="unsupported"):
            sync.synchronize(sync_context.remote.pk, uuid4())
        sync_context.remote.get_downloader.assert_not_called()
    assert HelmChartRemoteSerializer(
        data={"policy": "immediate"}, partial=True
    ).is_valid()


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientResponseError(
            SimpleNamespace(real_url="https://example.test"), (), status=500
        ),
        aiohttp.ClientResponseError(
            SimpleNamespace(real_url="https://example.test"), (), status=503
        ),
        aiohttp.ClientResponseError(
            SimpleNamespace(real_url="https://example.test"), (), status=429
        ),
        aiohttp.ClientOSError(104, "Connection reset by peer"),
        aiohttp.ServerDisconnectedError(),
        TimeoutError(),
    ],
)
@pytest.mark.parametrize("exhausted", [False, True])
def test_standard_downloader_retries(remote, monkeypatch, error, exhausted):
    # Exercise the real factory and retry wrapper; only transport and waiting are mocked.
    monkeypatch.setattr(
        DownloaderFactory,
        "_make_aiohttp_session_from_remote",
        Mock(return_value=Mock()),
    )
    monkeypatch.setattr("pulpcore.download.factory.atexit.register", Mock())
    monkeypatch.setattr("backoff._async.asyncio.sleep", AsyncMock())

    async def exercise():
        downloader = remote.get_downloader(url="https://example.test/chart.tgz")
        assert downloader.max_retries == 8
        result = object()
        transport = AsyncMock(side_effect=[error] * 9 if exhausted else [error, result])
        monkeypatch.setattr(downloader, "_run", transport)
        if exhausted:
            with pytest.raises(
                TimeoutException if isinstance(error, TimeoutError) else type(error)
            ):
                await downloader.run()
            assert transport.await_count == 9
        else:
            assert await downloader.run() is result
            assert transport.await_count == 2

    asyncio.run(exercise())


@pytest.fixture
def sync_context(remote, tmp_path, monkeypatch):
    bad = tmp_path / "bad.tgz"
    bad.write_bytes(b"mismatched archive must never reach content creation")
    good = tmp_path / "good.tgz"
    good.write_bytes(b"matching archive handled by mocked content creation")
    index = tmp_path / "index.yaml"
    index.write_text(
        yaml.safe_dump(
            {
                "entries": {
                    "alertmanager": [
                        {"version": "1.18.0", "digest": "a" * 64, "urls": ["bad.tgz"]}
                    ],
                    "other-chart": [
                        {
                            "version": "1.18.0",
                            "digest": hashlib.sha256(good.read_bytes()).hexdigest(),
                            "urls": ["good.tgz"],
                        }
                    ],
                }
            }
        )
    )
    downloads = {
        "https://example.test/" + name: Mock(
            fetch=Mock(return_value=SimpleNamespace(path=path))
        )
        for name, path in [("index.yaml", index), ("bad.tgz", bad), ("good.tgz", good)]
    }
    monkeypatch.setattr(
        remote, "get_downloader", Mock(side_effect=lambda url: downloads[url])
    )
    monkeypatch.setattr(HelmChartRemote.objects, "get", Mock(return_value=remote))
    locked = SimpleNamespace(auto_excluded_versions={}, save=Mock())
    lock_query = Mock(get=Mock(return_value=locked))
    monkeypatch.setattr(
        HelmChartRemote.objects, "select_for_update", Mock(return_value=lock_query)
    )
    monkeypatch.setattr(sync.transaction, "atomic", nullcontext)
    version = SimpleNamespace(pk=uuid4(), number=1, add_content=Mock(), content=Mock())
    version.content.filter.return_value.values_list.return_value = []
    repository = Mock()
    repository.new_version.return_value = nullcontext(version)
    repository.latest_version.return_value = version
    monkeypatch.setattr(
        sync.HelmChartRepository.objects, "get", Mock(return_value=repository)
    )
    monkeypatch.setattr(
        sync.HelmChartContent.objects,
        "filter",
        Mock(side_effect=lambda **kw: kw["pk__in"]),
    )
    monkeypatch.setattr(
        sync.RepositoryVersion.objects,
        "filter",
        Mock(return_value=Mock(first=Mock(return_value=None))),
    )
    upload = Mock(side_effect=lambda file: file)
    monkeypatch.setattr(sync.PulpTemporaryUploadedFile, "from_file", upload)
    create = Mock(
        return_value=SimpleNamespace(
            content=SimpleNamespace(
                pk="good-content", name="other-chart", version="1.18.0"
            ),
            created=True,
        )
    )
    monkeypatch.setattr(sync, "create_helmchart_content_from_tgz", create)
    return SimpleNamespace(
        remote=remote,
        locked=locked,
        repository=repository,
        version=version,
        downloads=downloads,
        create=create,
        upload=upload,
        index=index,
        bad=bad,
        good=good,
    )


def test_checksum_fail_never_creates_or_adds_content(sync_context):
    ctx = sync_context
    with pytest.raises(HelmChartError) as error:
        sync.synchronize(ctx.remote.pk, uuid4())
    for detail in (
        "alertmanager",
        "1.18.0",
        "https://example.test/bad.tgz",
        "a" * 64,
        hashlib.sha256(ctx.bad.read_bytes()).hexdigest(),
    ):
        assert detail in str(error.value)
    ctx.create.assert_not_called()
    ctx.upload.assert_not_called()
    ctx.version.add_content.assert_not_called()
    ctx.locked.save.assert_not_called()


@pytest.mark.parametrize("policy", ["skip", "exclude"])
def test_checksum_skip_and_exclude_continue_without_adding_mismatch(
    sync_context, caplog, policy
):
    ctx = sync_context
    ctx.remote.checksum_mismatch_policy = policy
    ctx.remote.exclude_versions = {"manual-chart": ["1.0.0"]}
    sync.synchronize(ctx.remote.pk, uuid4())
    ctx.create.assert_called_once()
    assert ctx.create.call_args.kwargs["relative_path"] == "good.tgz"
    ctx.upload.assert_called_once()
    assert ctx.upload.call_args.args[0].name.endswith("good.tgz")
    ctx.version.add_content.assert_called_once_with(["good-content"])
    assert ctx.remote.exclude_versions == {"manual-chart": ["1.0.0"]}
    for detail in (
        "alertmanager",
        "1.18.0",
        "https://example.test/bad.tgz",
        "a" * 64,
        hashlib.sha256(ctx.bad.read_bytes()).hexdigest(),
    ):
        assert detail in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)
    if policy == "exclude":
        ctx.locked.save.assert_called_once_with(
            update_fields=["auto_excluded_versions"]
        )
        metadata = ctx.locked.auto_excluded_versions["alertmanager"]["1.18.0"]
        assert metadata == {
            "reason": "checksum_mismatch",
            "expected": "a" * 64,
            "actual": hashlib.sha256(ctx.bad.read_bytes()).hexdigest(),
            "url": "https://example.test/bad.tgz",
            "timestamp": metadata["timestamp"],
        }
        # Reload the persisted JSON, as a subsequent sync would.
        ctx.remote.auto_excluded_versions = copy.deepcopy(
            ctx.locked.auto_excluded_versions
        )
    else:
        assert ctx.remote.auto_excluded_versions == {}
        ctx.locked.save.assert_not_called()
    sync.synchronize(ctx.remote.pk, uuid4())
    assert ctx.downloads["https://example.test/bad.tgz"].fetch.call_count == (
        1 if policy == "exclude" else 2
    )
    assert ctx.downloads["https://example.test/good.tgz"].fetch.call_count == 2


def test_all_auto_excluded_is_a_successful_noop(sync_context):
    ctx = sync_context
    ctx.remote.auto_excluded_versions = {
        "alertmanager": {"1.18.0": {}},
        "other-chart": {"1.18.0": {}},
    }
    sync.synchronize(ctx.remote.pk, uuid4())
    ctx.remote.get_downloader.assert_called_once_with(
        url="https://example.test/index.yaml"
    )
    ctx.create.assert_not_called()
    ctx.version.add_content.assert_not_called()


@pytest.mark.parametrize(
    "field, versions",
    [("include_versions", ["1.17.0"]), ("exclude_versions", ["1.18.0"])],
)
def test_sync_filters_versions_before_download_without_affecting_other_charts(
    sync_context, field, versions
):
    ctx = sync_context
    setattr(ctx.remote, field, {"alertmanager": versions})
    sync.synchronize(ctx.remote.pk, uuid4())
    ctx.downloads["https://example.test/bad.tgz"].fetch.assert_not_called()
    ctx.downloads["https://example.test/good.tgz"].fetch.assert_called_once()
    ctx.version.add_content.assert_called_once_with(["good-content"])


def test_duplicate_mismatch_is_auto_excluded_during_same_sync(sync_context):
    ctx = sync_context
    ctx.remote.checksum_mismatch_policy = "exclude"
    index = yaml.safe_load(ctx.index.read_text())
    index["entries"]["alertmanager"] *= 2
    ctx.index.write_text(yaml.safe_dump(index))
    sync.synchronize(ctx.remote.pk, uuid4())
    ctx.downloads["https://example.test/bad.tgz"].fetch.assert_called_once()
    ctx.locked.save.assert_called_once()
    ctx.version.add_content.assert_called_once_with(["good-content"])


def test_record_exclusion_merges_fresh_state_and_avoids_repeated_writes(sync_context):
    ctx = sync_context
    ctx.locked.auto_excluded_versions = {
        "concurrent-chart": {"2.0.0": {"reason": "checksum_mismatch"}}
    }
    entry = RepositoryChartEntry(
        "alertmanager", "1.18.0", ["bad.tgz"], "sha256:" + "a" * 64, {}
    )
    sync._record_auto_exclusion(
        ctx.remote, entry, "https://example.test/bad.tgz", "b" * 64
    )
    first = copy.deepcopy(ctx.locked.auto_excluded_versions)
    sync._record_auto_exclusion(
        ctx.remote, entry, "https://example.test/bad.tgz", "b" * 64
    )
    ctx.locked.save.assert_called_once()
    assert ctx.locked.auto_excluded_versions == first
    assert "concurrent-chart" in first
    sync._record_auto_exclusion(
        ctx.remote, entry, "https://example.test/bad.tgz", "c" * 64
    )
    assert ctx.locked.save.call_count == 2
    assert (
        ctx.locked.auto_excluded_versions["alertmanager"]["1.18.0"]["actual"]
        == "c" * 64
    )


def test_auto_exclusion_patch_clear_and_targeted_edit(sync_context):
    ctx = sync_context
    ctx.remote.checksum_mismatch_policy = "exclude"
    sync.synchronize(ctx.remote.pk, uuid4())
    mapping = copy.deepcopy(ctx.remote.auto_excluded_versions)
    for value in (mapping, {"alertmanager": {}}, {}):
        serializer = HelmChartRemoteSerializer(
            data={"auto_excluded_versions": value}, partial=True
        )
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["auto_excluded_versions"] == value
    with pytest.raises(ValidationError):
        HelmChartRemoteSerializer().fields["auto_excluded_versions"].run_validation(
            {"chart": ["1.0"]}
        )


@pytest.mark.parametrize("status", [403, 404, 410, 429, 500, 503])
@pytest.mark.parametrize("ignore", [True, False])
def test_final_download_failures_only_skip_unavailable(sync_context, status, ignore):
    ctx = sync_context
    ctx.remote.ignore_unavailable = ignore
    error = aiohttp.ClientResponseError(
        SimpleNamespace(real_url="https://example.test/bad.tgz"), (), status=status
    )
    downloader = ctx.downloads["https://example.test/bad.tgz"]
    downloader.fetch.side_effect = error
    if ignore and status in {403, 404, 410}:
        sync.synchronize(ctx.remote.pk, uuid4())
        ctx.version.add_content.assert_called_once_with(["good-content"])
    else:
        with pytest.raises(HelmChartError, match=f"HTTP {status}"):
            sync.synchronize(ctx.remote.pk, uuid4())
        ctx.create.assert_not_called()
        ctx.version.add_content.assert_not_called()
    # Sync calls fetch once; all retries belong to pulpcore.
    downloader.fetch.assert_called_once_with(
        extra_data={"request_kwargs": {"allow_redirects": False}}
    )
    ctx.locked.save.assert_not_called()


@pytest.mark.parametrize(
    "first", ["missing.tgz", "", "https://untrusted.example/chart.tgz", "retry.tgz"]
)
def test_sync_tries_safe_fallback_url_after_failure(sync_context, first):
    ctx = sync_context
    ctx.remote.exclude_charts = ["other-chart"]
    document = yaml.safe_load(ctx.index.read_text())
    document["entries"]["alertmanager"][0]["urls"] = [first, "working.tgz"]
    document["entries"]["alertmanager"][0]["digest"] = hashlib.sha256(
        ctx.good.read_bytes()
    ).hexdigest()
    ctx.index.write_text(yaml.safe_dump(document))
    ctx.downloads["https://example.test/working.tgz"] = Mock(
        fetch=Mock(return_value=SimpleNamespace(path=ctx.good))
    )
    if first in {"missing.tgz", "retry.tgz"}:
        status = 404 if first == "missing.tgz" else 503
        ctx.downloads[f"https://example.test/{first}"] = Mock(
            fetch=Mock(
                side_effect=aiohttp.ClientResponseError(
                    SimpleNamespace(real_url=f"https://example.test/{first}"),
                    (),
                    status=status,
                )
            )
        )
    ctx.create.return_value.content.name = "alertmanager"
    sync.synchronize(ctx.remote.pk, uuid4())
    ctx.create.assert_called_once()
    expected_filename = (
        first if first in {"missing.tgz", "retry.tgz"} else "working.tgz"
    )
    assert ctx.create.call_args.kwargs["relative_path"] == expected_filename
    ctx.downloads["https://example.test/working.tgz"].fetch.assert_called_once()
    if first == "":
        assert "https://example.test/" not in ctx.downloads
    if first.startswith("https://untrusted"):
        assert "https://untrusted.example/chart.tgz" not in ctx.downloads


@pytest.mark.parametrize("ignore", [False, True])
def test_all_urls_unavailable_obey_ignore_unavailable(sync_context, ignore):
    ctx = sync_context
    ctx.remote.ignore_unavailable = ignore
    ctx.remote.exclude_charts = ["other-chart"]
    document = yaml.safe_load(ctx.index.read_text())
    document["entries"]["alertmanager"][0]["urls"] = ["missing.tgz", "gone.tgz"]
    ctx.index.write_text(yaml.safe_dump(document))
    for name, status in (("missing.tgz", 404), ("gone.tgz", 410)):
        ctx.downloads[f"https://example.test/{name}"] = Mock(
            fetch=Mock(
                side_effect=aiohttp.ClientResponseError(
                    SimpleNamespace(real_url=f"https://example.test/{name}"),
                    (),
                    status=status,
                )
            )
        )
    if ignore:
        sync.synchronize(ctx.remote.pk, uuid4())
        assert ctx.repository.last_sync_details["charts_skipped_unavailable"] == 1
    else:
        with pytest.raises(HelmChartError, match="All 2 chart URLs failed"):
            sync.synchronize(ctx.remote.pk, uuid4())
    ctx.downloads["https://example.test/missing.tgz"].fetch.assert_called_once()
    ctx.downloads["https://example.test/gone.tgz"].fetch.assert_called_once()
    ctx.create.assert_not_called()


def test_checksum_mismatch_never_falls_back_to_second_url(sync_context):
    ctx = sync_context
    ctx.remote.checksum_mismatch_policy = "skip"
    ctx.remote.exclude_charts = ["other-chart"]
    document = yaml.safe_load(ctx.index.read_text())
    document["entries"]["alertmanager"][0]["urls"] = ["bad.tgz", "good.tgz"]
    ctx.index.write_text(yaml.safe_dump(document))
    sync.synchronize(ctx.remote.pk, uuid4())
    ctx.downloads["https://example.test/bad.tgz"].fetch.assert_called_once()
    ctx.downloads["https://example.test/good.tgz"].fetch.assert_not_called()
