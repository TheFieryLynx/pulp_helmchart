from __future__ import annotations

import json
import pprint
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Self


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        validate_assignment=True,
        protected_namespaces=(),
    )

    __properties: ClassVar[list[str]] = []

    def to_str(self) -> str:
        return pprint.pformat(self.to_dict())

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True)
        for field_name in self.model_fields_set:
            if getattr(self, field_name, None) is None:
                data[field_name] = None
        return data

    @classmethod
    def from_dict(cls, obj: Any) -> Self | None:
        if obj is None:
            return None
        if isinstance(obj, cls):
            return obj
        return cls.model_validate(obj)

    @classmethod
    def from_json(cls, json_str: str) -> Self | None:
        return cls.from_dict(json.loads(json_str))


class AsyncOperationResponse(_Model):
    task: str | None = None


class RepositoryVersionResponse(_Model):
    pulp_href: str | None = None
    pulp_created: str | None = None
    number: int | None = None
    repository: str | None = None
    base_version: str | None = None
    content_summary: dict[str, Any] | None = None


class HelmchartChartContent(_Model):
    repository: str | None = None
    artifact: str | None = None
    relative_path: str | None = None
    file: Any | None = None
    upload: str | None = None
    file_url: str | None = None
    downloader_config: Any | None = None
    pulp_labels: dict[str, str | None] | None = None


class HelmchartChartContentResponse(_Model):
    pulp_href: str | None = None
    pulp_created: str | None = None
    name: str | None = None
    version: str | None = None
    api_version: str | None = None
    app_version: str | None = None
    description: str | None = None
    digest: str | None = None
    filename: str | None = None
    chart_yaml: dict[str, Any] | None = None
    relative_path: str | None = None
    artifact: str | None = None
    pulp_labels: dict[str, str | None] | None = None


class HelmchartHelmchartRepository(_Model):
    pulp_labels: dict[str, str | None] | None = None
    name: str = Field(description="A unique name for this repository.")
    description: str | None = None
    retain_repo_versions: int | None = None
    remote: str | None = None
    autopublish: bool | None = False


class PatchedhelmchartHelmchartRepository(_Model):
    pulp_labels: dict[str, str | None] | None = None
    name: str | None = None
    description: str | None = None
    retain_repo_versions: int | None = None
    remote: str | None = None
    autopublish: bool | None = None


class HelmchartHelmchartRepositoryResponse(_Model):
    pulp_href: str | None = None
    pulp_created: str | None = None
    versions_href: str | None = None
    pulp_labels: dict[str, str | None] | None = None
    name: str | None = None
    description: str | None = None
    retain_repo_versions: int | None = None
    remote: str | None = None
    latest_version_href: str | None = None
    autopublish: bool | None = None
    last_sync_details: dict[str, Any] | None = None


class RepositoryAddRemoveContent(_Model):
    add_content_units: list[str] | None = None
    remove_content_units: list[str] | None = None
    base_version: str | None = None


class RepositorySyncURL(_Model):
    remote: str | None = None
    mirror: bool | None = False


class HelmchartHelmchartRemote(_Model):
    pulp_labels: dict[str, str | None] | None = None
    name: str
    url: str
    policy: str | None = "immediate"
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    tls_validation: bool | None = True
    proxy_url: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    username: str | None = None
    password: str | None = None
    max_retries: int | None = None
    total_timeout: float | None = None
    connect_timeout: float | None = None
    sock_connect_timeout: float | None = None
    sock_read_timeout: float | None = None
    headers: list[dict[str, str]] | None = None
    download_concurrency: int | None = None
    rate_limit: int | None = None
    include_charts: list[str] | None = None
    exclude_charts: list[str] | None = None
    include_versions: list[str] | None = None
    exclude_versions: list[str] | None = None
    latest_only: bool | None = False
    ignore_unavailable: bool | None = True


class PatchedhelmchartHelmchartRemote(_Model):
    pulp_labels: dict[str, str | None] | None = None
    name: str | None = None
    url: str | None = None
    policy: str | None = None
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    tls_validation: bool | None = None
    proxy_url: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    username: str | None = None
    password: str | None = None
    max_retries: int | None = None
    total_timeout: float | None = None
    connect_timeout: float | None = None
    sock_connect_timeout: float | None = None
    sock_read_timeout: float | None = None
    headers: list[dict[str, str]] | None = None
    download_concurrency: int | None = None
    rate_limit: int | None = None
    include_charts: list[str] | None = None
    exclude_charts: list[str] | None = None
    include_versions: list[str] | None = None
    exclude_versions: list[str] | None = None
    latest_only: bool | None = None
    ignore_unavailable: bool | None = None


class HelmchartHelmchartRemoteResponse(PatchedhelmchartHelmchartRemote):
    pulp_href: str | None = None
    pulp_created: str | None = None
    name: str | None = None
    url: str | None = None
    pulp_labels: dict[str, str | None] | None = None


class HelmchartHelmchartPublication(_Model):
    repository: str | None = None
    repository_version: str | None = None
    index: str | None = "index.yaml"
    checkpoint: bool | None = None


class HelmchartHelmchartPublicationResponse(_Model):
    pulp_href: str | None = None
    pulp_created: str | None = None
    repository_version: str | None = None
    repository: str | None = None
    distributions: list[str] | None = None
    index: str | None = None
    checkpoint: bool | None = None


class HelmchartHelmchartDistribution(_Model):
    name: str
    base_path: str
    pulp_labels: dict[str, str | None] | None = None
    publication: str | None = None
    repository: str | None = None
    repository_version: str | None = None
    remote: str | None = None
    content_guard: str | None = None
    hidden: bool | None = None
    checkpoint: bool | None = None


class PatchedhelmchartHelmchartDistribution(_Model):
    name: str | None = None
    base_path: str | None = None
    pulp_labels: dict[str, str | None] | None = None
    publication: str | None = None
    repository: str | None = None
    repository_version: str | None = None
    remote: str | None = None
    content_guard: str | None = None
    hidden: bool | None = None
    checkpoint: bool | None = None


class HelmchartHelmchartDistributionResponse(PatchedhelmchartHelmchartDistribution):
    pulp_href: str | None = None
    pulp_created: str | None = None
    base_url: str | None = None
    name: str | None = None
    base_path: str | None = None


class _PaginatedList(_Model):
    count: int = 0
    next: str | None = None
    previous: str | None = None
    results: list[Any] = Field(default_factory=list)


class PaginatedHelmchartChartContentResponseList(_PaginatedList):
    results: list[HelmchartChartContentResponse] = Field(default_factory=list)


class PaginatedHelmchartHelmchartRepositoryResponseList(_PaginatedList):
    results: list[HelmchartHelmchartRepositoryResponse] = Field(default_factory=list)


class PaginatedHelmchartHelmchartRemoteResponseList(_PaginatedList):
    results: list[HelmchartHelmchartRemoteResponse] = Field(default_factory=list)


class PaginatedHelmchartHelmchartPublicationResponseList(_PaginatedList):
    results: list[HelmchartHelmchartPublicationResponse] = Field(default_factory=list)


class PaginatedHelmchartHelmchartDistributionResponseList(_PaginatedList):
    results: list[HelmchartHelmchartDistributionResponse] = Field(default_factory=list)


class PaginatedRepositoryVersionResponseList(_PaginatedList):
    results: list[RepositoryVersionResponse] = Field(default_factory=list)
