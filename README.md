# pulp-helmchart

Minimal Pulp plugin for classic Helm chart repositories.

The first implementation mirrors the built-in `pulp_file` plugin shape:

* upload packaged chart archives (`*.tgz`)
* parse chart metadata from `Chart.yaml`
* create repository versions
* create publications
* generate Helm-compatible `index.yaml`
* serve `index.yaml` and chart archives through normal Pulp content distributions

It intentionally does not implement upstream sync, provenance files, signing, OCI Helm support,
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
