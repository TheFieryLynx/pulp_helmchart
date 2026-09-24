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

The upload returns a Pulp task (HTTP 202). Wait for it to complete and read the created
content href from its created resources. Supplying `repository` requires both repository
`modify` and `view` permissions; Pulp reserves that repository for the upload task.
Archives must have one chart root with `Chart.yaml` directly beneath it and a valid Helm chart
name and semantic version. Helm-compatible prerelease, build metadata, short versions, and
`v`-prefixed versions retain their original spelling. Invalid packages create no chart content.
`Chart.yaml` is limited to 2 MiB after decompression, and an archive may contain at most
10,000 tar members. These generous limits bound metadata parsing and publication index size.
Using an existing Pulp Upload also requires `core.change_upload` permission on that Upload.

## Manual Helm validation

Fetch a test chart:

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update
helm pull nvidia/gpu-operator --version v26.3.3
```

Upload/publish/distribute it through Pulp, then verify:

```bash
helm repo add nvidia-pulp "$DISTRIBUTION_BASE_URL"
helm repo update
helm search repo nvidia-pulp/gpu-operator --versions
helm template gpu-operator nvidia-pulp/gpu-operator --version v26.3.3
```

For a distribution whose returned `base_url` is
`https://mirror.intra.acloud.ru/repos/helm/nvidia/`, the content paths are:

```text
/repos/helm/nvidia/index.yaml
/repos/helm/nvidia/gpu-operator-v26.3.3.tgz
```

Always use the distribution's `base_url`; the content prefix is configurable per Pulp
deployment.

## Implementation notes

This plugin is a near-copy/adaptation of upstream `pulp_file` concepts. It keeps pass-through
publication of the uploaded archives and replaces `PULP_MANIFEST` generation with `index.yaml`.

The generated `index.yaml` uses relative URLs by default, for example:

```yaml
urls:
  - gpu-operator-v26.3.3.tgz
```

Helm publications always place `index.yaml` at the distribution root alongside chart archives.
Requests to publish at a nested index path are rejected. Publications accept only Helm chart
repositories or their versions; distributions accept only Helm chart repositories, versions,
or publications. Existing historical records remain readable.

For each chart, generated index entries are ordered by Helm-compatible semantic version
precedence, newest first. Build metadata does not affect precedence; stored version text is
preserved. Legacy content with an invalid version sorts after valid versions with a warning.

## Sync remote configuration

Create a remote with `POST /pulp/api/v3/remotes/helmchart/helmchart/`, or update its
returned `pulp_href` with `PATCH`. For example:

```json
{
  "name": "public-charts",
  "url": "https://prometheus-community.github.io/helm-charts/",
  "policy": "immediate",
  "allowed_chart_hosts": ["github.com", "release-assets.githubusercontent.com"],
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
retry loop. Helm remotes support only `policy: immediate`; `on_demand` and `streamed` are rejected.
An older remote stored with either unsupported policy fails before its index is downloaded.

If an index entry lists multiple archive URLs, sync tries them in declared order. Blank URLs are
discarded. Unsafe URLs are rejected individually, and an unavailable or retry-exhausted URL can
fall back to the next safe URL. Once bytes download successfully, a digest mismatch applies the
configured checksum policy immediately; sync does not try another URL for that mismatch. If all
candidates fail, sync fails except when every attempted download returned HTTP 403, 404, or 410
and `ignore_unavailable` (default `true`) is enabled. Rejected URLs alone never cause a skip.
The published archive filename comes from the first URL allowed by the remote policy, so a
preferred mirror recovering later does not change an existing chart content path.

By default, a Helm remote downloads charts only from its own origin (same scheme, hostname,
and port). Relative chart URLs and absolute same-origin HTTP(S) URLs work without configuration.
Some public Helm repositories host archives elsewhere: Prometheus Community uses `github.com`,
which redirects release downloads to `release-assets.githubusercontent.com`. List **both** exact
hostnames in `allowed_chart_hosts` to trust that download chain. The field defaults to `[]`;
hostnames are case-insensitive and match exactly, with no wildcard or subdomain matching. The
allowlist trusts HTTP(S) on any port of a listed hostname. Every redirect is checked against
the remote origin and this list; an unlisted host, unsupported scheme, embedded URL credentials,
redirect loop, or excessive redirect chain is rejected. The configured remote's Basic Auth,
custom headers (including cookies and tokens), client certificate, and proxy credentials are
used only for same-origin requests. Cross-origin downloads use pulpcore retry, timeout, and TLS
settings with no custom remote headers or proxy. If a trusted host needs its own authentication,
use a separate remote pointed at that origin. URLs written to checksum diagnostics omit embedded
credentials, query parameters, and fragments.

A remote URL may point directly at `index.yaml?token=...` or at a repository base URL with a
query token. The index request retains that query. Relative chart URLs resolve against the
index path **without inheriting its query token**; chart archive URLs must include their own
query authentication if required. The token is never copied to another origin. Remote URL
queries are redacted from sync diagnostics.

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

`latest_only`, when enabled, selects the highest semantic version per chart after these
filters, regardless of upstream entry order. Prereleases remain eligible and compare by SemVer
precedence; there is no stable-only mode. If no entries remain, sync completes without adding
content. Sync is additive:
filters and checksum policies do not remove content already present in the repository.
Sync task progress reports show index parsing, selected versions, chart downloads, skips,
automatic exclusions, and created or reused content. Downloads remain serial.

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

For a targeted retry, POST to `<remote pulp_href>retry_auto_exclusion/` with
`{"chart": "alertmanager", "version": "1.18.0"}`. This returns a Pulp task (HTTP 202).
The task reserves the remote and removes only that chart/version from the latest stored
mapping. Repeating the action after the entry is absent succeeds. Manual exclusions still
apply. General PATCH of the complete mapping remains available for administrative changes;
it replaces the JSON field and should not be used for targeted retries during sync.

### Content with multiple archive paths

Each chart content object has one canonical archive filename. Uploading identical bytes under
another filename is rejected. If an older installation already has multiple `ContentArtifact`
paths for one chart, reuse and publication fail with an ambiguity error. To repair it, first
identify every repository and publication referencing the content, choose the intended archive
path from the original chart filename and existing published URLs, then remove the unwanted
`ContentArtifact` rows under an operator-controlled maintenance window. Rebuild affected
publications after confirming their existing URLs. The plugin does not guess or delete paths.

Migration `0006_remote_sync_policies` is preserved as shipped. It converts empty legacy global
version lists to `{}` and nonempty lists to `{"*": [...]}`. New sync code understands both
legacy lists and mappings, including legacy lists encountered during transition.

**Upgrading a database that has not yet applied 0006 requires a full stop:** stop all Pulp API,
content, and worker processes, wait for their status heartbeats to expire, install the new plugin,
run `pulpcore-manager migrate`, and then start only processes running the new plugin. The plugin's
pre-migration guard refuses 0006 while any Pulp process still has a live status heartbeat.
Do not perform a rolling code/schema upgrade across 0006. On a database where 0006 is already
applied, the guard is inactive; normal forward migrations can proceed. This guard cannot undo
incorrect syncs that may have occurred in an earlier mixed-version deployment.

## Python SDK client

The database regression tests require an isolated PostgreSQL test database and a `PULP_SETTINGS`
file pointing to it. Run `PULP_SETTINGS=/path/to/test-settings.py pytest` to include them; plain
`pytest` skips those integration cases when no test settings file is configured.

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
