from __future__ import annotations

import json
from base64 import b64encode
from typing import Any
from urllib.parse import quote, urlencode

import urllib3
from pulpcore.client.pulpcore import ApiClient
from pulpcore.client.pulpcore.exceptions import ApiException

from .models import (
    AsyncOperationResponse,
    HelmchartChartContentResponse,
    HelmchartHelmchartDistribution,
    HelmchartHelmchartDistributionResponse,
    HelmchartHelmchartPublication,
    HelmchartHelmchartPublicationResponse,
    HelmchartHelmchartRemote,
    HelmchartHelmchartRemoteResponse,
    HelmchartHelmchartRepository,
    HelmchartHelmchartRepositoryResponse,
    PaginatedHelmchartChartContentResponseList,
    PaginatedHelmchartHelmchartDistributionResponseList,
    PaginatedHelmchartHelmchartPublicationResponseList,
    PaginatedHelmchartHelmchartRemoteResponseList,
    PaginatedHelmchartHelmchartRepositoryResponseList,
    PaginatedRepositoryVersionResponseList,
    PatchedhelmchartHelmchartDistribution,
    PatchedhelmchartHelmchartRemote,
    PatchedhelmchartHelmchartRepository,
    RepositoryAddRemoveContent,
    RepositorySyncURL,
    RepositoryVersionResponse,
)


class _Http:
    def __init__(self, api_client: ApiClient | None = None) -> None:
        self.api_client = api_client or ApiClient.get_default()
        self.configuration = self.api_client.configuration
        self.pool = urllib3.PoolManager(
            cert_reqs="CERT_REQUIRED" if getattr(self.configuration, "verify_ssl", True) else "CERT_NONE",
            ca_certs=getattr(self.configuration, "ssl_ca_cert", None),
            timeout=getattr(self.configuration, "timeout", None),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        fields: dict[str, Any] | None = None,
        response_model: type | None = None,
    ) -> Any:
        url = self._url(path, query)
        headers = self._headers()
        request_kwargs: dict[str, Any] = {"headers": headers}
        if fields is not None:
            request_kwargs["fields"] = fields
            request_kwargs["encode_multipart"] = True
        elif body is not None:
            headers["Content-Type"] = "application/json"
            request_kwargs["body"] = json.dumps(_to_payload(body)).encode()

        response = self.pool.request(method, url, **request_kwargs)
        if response.status >= 400:
            raise ApiException(status=response.status, reason=response.data.decode(errors="replace"))
        if not response.data:
            return None
        payload = json.loads(response.data.decode())
        if response_model is None:
            return payload
        return response_model.from_dict(payload)

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        host = self.configuration.host.rstrip("/")
        url = host + path
        clean_query = _clean_query(query or {})
        if clean_query:
            url += "?" + urlencode(clean_query, doseq=True)
        return url

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "OpenAPI-Generator/0.2.0.dev0/python",
        }
        username = getattr(self.configuration, "username", None)
        password = getattr(self.configuration, "password", None)
        if username is not None and password is not None:
            token = b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        return headers


class _CrudApi:
    path = ""
    href_arg = "pulp_href"
    create_model = None
    update_model = None
    response_model = None
    page_model = None

    def __init__(self, api_client: ApiClient | None = None) -> None:
        self._http = _Http(api_client)

    def list(self, **kwargs: Any) -> Any:
        return self._http.request("GET", self.path, query=kwargs, response_model=self.page_model)

    def read(self, **kwargs: Any) -> Any:
        href = _href_from_kwargs(kwargs, self.href_arg)
        return self._http.request("GET", href, response_model=self.response_model)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        body = _single_body(args, kwargs)
        return self._http.request("POST", self.path, body=body, response_model=self._mutation_response_model)

    def update(self, *args: Any, **kwargs: Any) -> Any:
        href, body = _href_and_body(args, kwargs, self.href_arg)
        return self._http.request("PUT", href, body=body, response_model=self._mutation_response_model)

    def partial_update(self, *args: Any, **kwargs: Any) -> Any:
        href, body = _href_and_body(args, kwargs, self.href_arg)
        return self._http.request("PATCH", href, body=body, response_model=self._mutation_response_model)

    def delete(self, **kwargs: Any) -> Any:
        href = _href_from_kwargs(kwargs, self.href_arg)
        return self._http.request("DELETE", href, response_model=AsyncOperationResponse)

    @property
    def _mutation_response_model(self) -> type:
        return self.response_model


class RepositoriesHelmchartApi(_CrudApi):
    path = "/pulp/api/v3/repositories/helmchart/helmchart/"
    href_arg = "helmchart_helmchart_repository_href"
    create_model = HelmchartHelmchartRepository
    update_model = PatchedhelmchartHelmchartRepository
    response_model = HelmchartHelmchartRepositoryResponse
    page_model = PaginatedHelmchartHelmchartRepositoryResponseList

    def create(
        self,
        helmchart_helmchart_repository: HelmchartHelmchartRepository,
        **_: Any,
    ) -> HelmchartHelmchartRepositoryResponse:
        return self._http.request(
            "POST",
            self.path,
            body=helmchart_helmchart_repository,
            response_model=HelmchartHelmchartRepositoryResponse,
        )

    def read(
        self,
        helmchart_helmchart_repository_href: str,
        **_: Any,
    ) -> HelmchartHelmchartRepositoryResponse:
        return self._http.request(
            "GET",
            helmchart_helmchart_repository_href,
            response_model=HelmchartHelmchartRepositoryResponse,
        )

    def partial_update(
        self,
        helmchart_helmchart_repository_href: str,
        patchedhelmchart_helmchart_repository: PatchedhelmchartHelmchartRepository,
        **_: Any,
    ) -> HelmchartHelmchartRepositoryResponse:
        return self._http.request(
            "PATCH",
            helmchart_helmchart_repository_href,
            body=patchedhelmchart_helmchart_repository,
            response_model=HelmchartHelmchartRepositoryResponse,
        )

    def update(
        self,
        helmchart_helmchart_repository_href: str,
        helmchart_helmchart_repository: HelmchartHelmchartRepository,
        **_: Any,
    ) -> HelmchartHelmchartRepositoryResponse:
        return self._http.request(
            "PUT",
            helmchart_helmchart_repository_href,
            body=helmchart_helmchart_repository,
            response_model=HelmchartHelmchartRepositoryResponse,
        )

    def delete(
        self,
        helmchart_helmchart_repository_href: str,
        **_: Any,
    ) -> AsyncOperationResponse:
        return self._http.request(
            "DELETE",
            helmchart_helmchart_repository_href,
            response_model=AsyncOperationResponse,
        )

    def modify(
        self,
        helmchart_helmchart_repository_href: str,
        repository_add_remove_content: RepositoryAddRemoveContent,
        **_: Any,
    ) -> AsyncOperationResponse:
        path = _join_href(helmchart_helmchart_repository_href, "modify/")
        return self._http.request(
            "POST",
            path,
            body=repository_add_remove_content,
            response_model=AsyncOperationResponse,
        )

    def sync(
        self,
        helmchart_helmchart_repository_href: str,
        repository_sync_url: RepositorySyncURL | None = None,
        **kwargs: Any,
    ) -> AsyncOperationResponse:
        path = _join_href(helmchart_helmchart_repository_href, "sync/")
        body = repository_sync_url or RepositorySyncURL(
            remote=kwargs.get("remote"),
            mirror=kwargs.get("mirror"),
        )
        return self._http.request(
            "POST",
            path,
            body=body,
            response_model=AsyncOperationResponse,
        )


class RepositoriesHelmchartVersionsApi(_CrudApi):
    href_arg = "repository_version_href"
    response_model = RepositoryVersionResponse
    page_model = PaginatedRepositoryVersionResponseList

    def list(self, helmchart_helmchart_repository_href: str, **kwargs: Any) -> Any:
        return self._http.request(
            "GET",
            _join_href(helmchart_helmchart_repository_href, "versions/"),
            query=kwargs,
            response_model=self.page_model,
        )

    def read(self, repository_version_href: str, **_: Any) -> Any:
        return self._http.request("GET", repository_version_href, response_model=self.response_model)


class ContentChartsApi(_CrudApi):
    path = "/pulp/api/v3/content/helmchart/chart/"
    href_arg = "helmchart_chart_content_href"
    response_model = HelmchartChartContentResponse
    page_model = PaginatedHelmchartChartContentResponseList

    def create(self, **kwargs: Any) -> AsyncOperationResponse:
        fields = _content_fields(kwargs)
        return self._http.request("POST", self.path, fields=fields, response_model=AsyncOperationResponse)

    def upload(self, **kwargs: Any) -> HelmchartChartContentResponse:
        fields = _content_fields(kwargs)
        return self._http.request(
            "POST",
            _join_href(self.path, "upload/"),
            fields=fields,
            response_model=HelmchartChartContentResponse,
        )


ContentFilesApi = ContentChartsApi


class RemotesHelmchartApi(_CrudApi):
    path = "/pulp/api/v3/remotes/helmchart/helmchart/"
    href_arg = "helmchart_helmchart_remote_href"
    create_model = HelmchartHelmchartRemote
    update_model = PatchedhelmchartHelmchartRemote
    response_model = HelmchartHelmchartRemoteResponse
    page_model = PaginatedHelmchartHelmchartRemoteResponseList

    def create(
        self,
        helmchart_helmchart_remote: HelmchartHelmchartRemote,
        **_: Any,
    ) -> HelmchartHelmchartRemoteResponse:
        return self._http.request(
            "POST",
            self.path,
            body=helmchart_helmchart_remote,
            response_model=HelmchartHelmchartRemoteResponse,
        )

    def read(self, helmchart_helmchart_remote_href: str, **_: Any) -> HelmchartHelmchartRemoteResponse:
        return self._http.request("GET", helmchart_helmchart_remote_href, response_model=HelmchartHelmchartRemoteResponse)

    def partial_update(
        self,
        helmchart_helmchart_remote_href: str,
        patchedhelmchart_helmchart_remote: PatchedhelmchartHelmchartRemote,
        **_: Any,
    ) -> HelmchartHelmchartRemoteResponse:
        return self._http.request(
            "PATCH",
            helmchart_helmchart_remote_href,
            body=patchedhelmchart_helmchart_remote,
            response_model=HelmchartHelmchartRemoteResponse,
        )

    def delete(self, helmchart_helmchart_remote_href: str, **_: Any) -> AsyncOperationResponse:
        return self._http.request("DELETE", helmchart_helmchart_remote_href, response_model=AsyncOperationResponse)


class PublicationsHelmchartApi(_CrudApi):
    path = "/pulp/api/v3/publications/helmchart/helmchart/"
    href_arg = "helmchart_helmchart_publication_href"
    create_model = HelmchartHelmchartPublication
    response_model = HelmchartHelmchartPublicationResponse
    page_model = PaginatedHelmchartHelmchartPublicationResponseList

    @property
    def _mutation_response_model(self) -> type:
        return AsyncOperationResponse

    def create(
        self,
        helmchart_helmchart_publication: HelmchartHelmchartPublication,
        **_: Any,
    ) -> AsyncOperationResponse:
        return self._http.request(
            "POST",
            self.path,
            body=helmchart_helmchart_publication,
            response_model=AsyncOperationResponse,
        )

    def read(
        self,
        helmchart_helmchart_publication_href: str,
        **_: Any,
    ) -> HelmchartHelmchartPublicationResponse:
        return self._http.request(
            "GET",
            helmchart_helmchart_publication_href,
            response_model=HelmchartHelmchartPublicationResponse,
        )

    def delete(
        self,
        helmchart_helmchart_publication_href: str,
        **_: Any,
    ) -> AsyncOperationResponse:
        return self._http.request(
            "DELETE",
            helmchart_helmchart_publication_href,
            response_model=AsyncOperationResponse,
        )


class DistributionsHelmchartApi(_CrudApi):
    path = "/pulp/api/v3/distributions/helmchart/helmchart/"
    href_arg = "helmchart_helmchart_distribution_href"
    create_model = HelmchartHelmchartDistribution
    update_model = PatchedhelmchartHelmchartDistribution
    response_model = HelmchartHelmchartDistributionResponse
    page_model = PaginatedHelmchartHelmchartDistributionResponseList

    @property
    def _mutation_response_model(self) -> type:
        return AsyncOperationResponse

    def create(
        self,
        helmchart_helmchart_distribution: HelmchartHelmchartDistribution,
        **_: Any,
    ) -> AsyncOperationResponse:
        return self._http.request(
            "POST",
            self.path,
            body=helmchart_helmchart_distribution,
            response_model=AsyncOperationResponse,
        )

    def read(
        self,
        helmchart_helmchart_distribution_href: str,
        **_: Any,
    ) -> HelmchartHelmchartDistributionResponse:
        return self._http.request(
            "GET",
            helmchart_helmchart_distribution_href,
            response_model=HelmchartHelmchartDistributionResponse,
        )

    def partial_update(
        self,
        helmchart_helmchart_distribution_href: str,
        patchedhelmchart_helmchart_distribution: PatchedhelmchartHelmchartDistribution,
        **_: Any,
    ) -> AsyncOperationResponse:
        return self._http.request(
            "PATCH",
            helmchart_helmchart_distribution_href,
            body=patchedhelmchart_helmchart_distribution,
            response_model=AsyncOperationResponse,
        )

    def update(
        self,
        helmchart_helmchart_distribution_href: str,
        helmchart_helmchart_distribution: HelmchartHelmchartDistribution,
        **_: Any,
    ) -> AsyncOperationResponse:
        return self._http.request(
            "PUT",
            helmchart_helmchart_distribution_href,
            body=helmchart_helmchart_distribution,
            response_model=AsyncOperationResponse,
        )

    def delete(
        self,
        helmchart_helmchart_distribution_href: str,
        **_: Any,
    ) -> AsyncOperationResponse:
        return self._http.request(
            "DELETE",
            helmchart_helmchart_distribution_href,
            response_model=AsyncOperationResponse,
        )


def _clean_query(query: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in query.items():
        if key.startswith("_") or value is None:
            continue
        result[key] = value
    return result


def _to_payload(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True)
    return value


def _single_body(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if args:
        return args[0]
    values = {key: value for key, value in kwargs.items() if not key.startswith("_")}
    if len(values) == 1:
        return next(iter(values.values()))
    return values


def _href_and_body(args: tuple[Any, ...], kwargs: dict[str, Any], href_arg: str) -> tuple[str, Any]:
    href = kwargs.pop(href_arg, None)
    if href is None and args:
        href = args[0]
        args = args[1:]
    if href is None:
        href = kwargs.pop("pulp_href", None)
    if href is None:
        raise TypeError(f"Missing required href argument '{href_arg}'")
    body = args[0] if args else _single_body((), kwargs)
    return href, body


def _href_from_kwargs(kwargs: dict[str, Any], href_arg: str) -> str:
    href = kwargs.get(href_arg) or kwargs.get("pulp_href")
    if not href:
        raise TypeError(f"Missing required href argument '{href_arg}'")
    return href


def _join_href(href: str, suffix: str) -> str:
    if href.startswith("/"):
        base = href
    else:
        base = "/" + href
    if not base.endswith("/"):
        base += "/"
    return base + suffix


def _content_fields(kwargs: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key.startswith("_") or value is None:
            continue
        if key == "file":
            fields[key] = _file_field(value)
        elif hasattr(value, "to_dict") or hasattr(value, "model_dump"):
            fields[key] = json.dumps(_to_payload(value))
        else:
            fields[key] = value
    return fields


def _file_field(value: Any) -> Any:
    if isinstance(value, tuple):
        if len(value) == 2:
            return value
        if len(value) >= 3:
            return value
    if isinstance(value, bytes):
        return ("chart.tgz", value, "application/gzip")
    if isinstance(value, str):
        with open(value, "rb") as handle:
            return (value.rsplit("/", 1)[-1], handle.read(), "application/gzip")
    return value
