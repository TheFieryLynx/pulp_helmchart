# pulp-helmchart-client

Python SDK client for the `pulp-helmchart` Pulp plugin.

It exposes the expected Pulp client namespace:

```python
from pulpcore.client import pulp_helmchart

configuration = pulp_helmchart.Configuration(host="http://localhost:24817", username="admin", password="password")
api_client = pulp_helmchart.ApiClient(configuration)
repositories = pulp_helmchart.RepositoriesHelmchartApi(api_client)
```

This package is intentionally small and mirrors the generated-client surface needed by Pulp UI.
Requests use the supplied pulpcore `ApiClient` transport, including its configured TLS, proxy,
default headers, Basic Auth, and cookie authentication. A configured access token is sent as a
Bearer token when Basic Auth is not configured. A filesystem path passed as `file` is uploaded
through Pulp's chunked Upload API in 4 MiB pieces before chart creation; that caller needs
Pulp Upload create/update permission. An unconsumed Upload is deleted if chart dispatch fails.

`ContentChartsApi.upload(...)` returns an `AsyncOperationResponse` with a task href. When a
repository is supplied, wait for that task before reading the new repository version. Uploaders
need chart upload permission plus repository modify and view permission.

`RemotesHelmchartApi.retry_auto_exclusion(remote_href, chart, version)` returns a task that
atomically removes one automatic checksum exclusion. It succeeds when the entry is already gone.

`HelmchartHelmchartRemote.allowed_chart_hosts` accepts exact hostnames for chart archives hosted
outside the remote origin. For Prometheus Community release assets, use `github.com` and
`release-assets.githubusercontent.com`. Remote credentials and custom headers stay on the remote
origin; cross-origin chart downloads are anonymous.

Helm remote client models accept only `policy="immediate"`. Publication `index` is fixed at
`index.yaml`; the API rejects nested paths and cross-plugin publication/distribution associations.

Use the distribution's returned `base_url` with `helm repo add`; its content prefix is set by
the Pulp deployment and need not be `/pulp/content/`.
