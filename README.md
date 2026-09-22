# pulp-helmchart

Minimal Pulp plugin for classic Helm chart repositories.

The first implementation mirrors the built-in `pulp_file` plugin shape:

* upload packaged chart archives (`*.tgz`)
* sync classic upstream Helm repositories
* parse chart metadata from `Chart.yaml`
* create repository versions
* create publications
* generate Helm-compatible `index.yaml`
* serve `index.yaml` and chart archives through normal Pulp content distributions

It intentionally does not implement provenance files, signing, OCI Helm support,
ChartMuseum APIs, container image mirroring, Harbor integration, or a web UI.

## Expected API / CLI shape

The plugin exposes Pulp viewsets under the `helmchart` namespace. Generated CLI command names
depend on the installed `pulp-cli` and generated bindings, but the intended flow is:

```bash
pulp helmchart repository create --name nvidia
pulp helmchart chart create --repository nvidia --file gpu-operator-v26.3.3.tgz
pulp helmchart publication create --repository nvidia
pulp helmchart distribution create \
  --name nvidia \
  --base-path helm/nvidia \
  --repository nvidia
```

If your CLI exposes the upload action separately, use:

```bash
pulp helmchart chart upload --file gpu-operator-v26.3.3.tgz
```

and then add the returned content to a repository with the repository `modify` action.

## Manual Helm validation

Fetch a test chart:

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update
helm pull nvidia/gpu-operator --version v26.3.3
```

Upload/publish/distribute it through Pulp, then verify:

```bash
helm repo add nvidia-pulp https://mirror.intra.acloud.ru/pulp/content/helm/nvidia
helm repo update
helm search repo nvidia-pulp/gpu-operator --versions
helm template gpu-operator nvidia-pulp/gpu-operator --version v26.3.3
```

Expected content paths:

```text
/pulp/content/helm/nvidia/index.yaml
/pulp/content/helm/nvidia/gpu-operator-v26.3.3.tgz
```

## Implementation notes

This plugin is a near-copy/adaptation of upstream `pulp_file` concepts. It keeps pass-through
publication of the uploaded archives and replaces `PULP_MANIFEST` generation with `index.yaml`.

The generated `index.yaml` uses relative URLs by default, for example:

```yaml
urls:
  - gpu-operator-v26.3.3.tgz
```

## Sync remote configuration

Create a remote with `POST /pulp/api/v3/remotes/helmchart/helmchart/`, or update its
returned `pulp_href` with `PATCH`. For example:

```json
{
  "name": "public-charts",
  "url": "https://prometheus-community.github.io/helm-charts/",
  "max_retries": 8,
  "ignore_unavailable": true,
  "checksum_mismatch_policy": "exclude",
  "include_versions": {},
  "exclude_versions": {}
}
```

`max_retries` is inherited from pulpcore's `Remote` and already exposed by the standard
remote serializer. Both index and archive downloads use pulpcore's HTTP downloader, including
its exponential backoff for HTTP 5xx/429, connection failures, and timeouts. There is no plugin
retry loop. A final failure propagates and fails sync. `ignore_unavailable` (default `true`)
only allows skipping archive HTTP 403, 404, and 410; it never skips 5xx or checksum failures.

Filters apply in this order: `include_charts`, `exclude_charts`, `include_versions`,
`exclude_versions`, then `auto_excluded_versions`. Chart-name lists use exact matches; an
empty `include_charts` list allows every chart. Version filters are mappings of chart names to
lists of exact version strings, without semver/range matching:

```json
{
  "include_versions": {
    "alertmanager": ["1.18.0", "1.17.0"],
    "prometheus": ["27.0.0"]
  },
  "exclude_versions": {
    "alertmanager": ["1.18.0"]
  }
}
```

This selects only `alertmanager 1.17.0` and `prometheus 27.0.0` from those charts; it does
not restrict other charts. Excluding `alertmanager 1.18.0` never excludes another chart's
`1.18.0`. An empty include list for a named chart selects none of its versions.

`"*"` is the backward-compatible global version-filter key. For includes, a chart-specific
entry overrides the wildcard fallback. For excludes, wildcard and chart-specific lists are
combined. With neither key present, that filter imposes no version restriction:

```json
{
  "include_versions": {"*": ["1.18.0"], "prometheus": ["27.0.0"]},
  "exclude_versions": {"*": ["1.16.0"], "alertmanager": ["1.18.0"]}
}
```

`latest_only`, when enabled, keeps the first eligible index entry per chart after these
filters. If no entries remain, sync completes without adding content. Sync is additive:
filters and checksum policies do not remove content already present in the repository.

### Checksum policy and recovery

`checksum_mismatch_policy` accepts:

* `fail` (default): fail sync with chart name, version, URL, expected digest, and actual digest.
* `skip`: warn and skip the version for this sync; try it again next time without changing the remote.
* `exclude`: warn, skip, and persist an automatic exclusion for this chart and version.

None of these policies accepts mismatched bytes or creates content from them. The existing
behavior for index entries without a digest is unchanged. Automatic exclusions are separate
from user-managed `exclude_versions` and contain diagnostics such as:

```json
{
  "auto_excluded_versions": {
    "alertmanager": {
      "1.18.0": {
        "reason": "checksum_mismatch",
        "expected": "d21671e85757c43da3732954502ccb0597b680ea74b7dfd645d9453a0343f8b4",
        "actual": "803c78802591e0fc7dfc6e8f1cdb159b62b158124977f50831c9947dd12c2303",
        "url": "https://example.com/alertmanager-1.18.0.tgz",
        "timestamp": "2026-09-22T12:00:00+00:00"
      }
    }
  }
}
```

Automatic exclusions always apply, even after changing the policy, until explicitly removed.
They use exact chart/version keys; `"*"` wildcard rules apply only to the manual version filters.
Concurrent syncs merge automatic exclusions under a database row lock. An identical recorded
mismatch does not cause another write or timestamp change.

To retry all automatically excluded versions, PATCH the remote with:

```json
{"auto_excluded_versions": {}}
```

For a targeted retry, GET the remote, remove the desired chart/version entry from its
`auto_excluded_versions`, then PATCH the complete remaining mapping. This replaces that JSON
field rather than merging it. Manual exclusions still apply. No automatic exclusion is removed
just because the upstream artifact or digest changes.

Migration `0006_remote_sync_policies` converts empty legacy global version lists to `{}` and
nonempty lists to `{"*": [...]}`, preserving their global scope. It adds the policy and
automatic-exclusion fields; existing remotes default to `fail` with no automatic exclusions.
Run the normal `pulpcore-manager migrate` during upgrade. Existing migrations are unchanged.

## Python SDK client

This repository also contains `client/`, a separate installable Python package:

```text
pulp-helmchart-client
```

Install from this repository with:

```bash
pip install "pulp-helmchart-client @ git+ssh://git@github.com/TheFieryLynx/pulp_helmchart.git@main#subdirectory=client"
```

The import namespace matches Pulp generated clients:

```python
from pulpcore.client import pulp_helmchart

pulp_helmchart.ApiClient
pulp_helmchart.Configuration
pulp_helmchart.RepositoriesHelmchartApi
pulp_helmchart.ContentChartsApi
pulp_helmchart.ContentFilesApi
pulp_helmchart.PublicationsHelmchartApi
pulp_helmchart.DistributionsHelmchartApi
```
