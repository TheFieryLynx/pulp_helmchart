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
