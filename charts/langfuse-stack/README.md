# langfuse-stack

A thin wrapper around the official [`langfuse`](https://github.com/langfuse/langfuse-k8s)
chart whose single job is to make sure **an upgrade can never lose data**.

It does not replace the upstream chart — it depends on it, unmodified. A fork
would have to be re-merged on every Langfuse release, which is how this
deployment ended up five weeks behind upstream and silently pricing several
models at `$0`.

## What it adds

**1. ClickHouse the chart cannot delete.** This chart templates the
`KeeperCluster` + `ClickHouseCluster` resources for the
[ClickHouse Kubernetes Operator](https://github.com/ClickHouse/clickhouse-operator),
following upstream's own `examples/v4-installation`. Both carry
`helm.sh/resource-policy: keep`, and the operator — not Helm — owns the
StatefulSets and PVCs, so uninstalling the release cannot delete the traces.

Since upstream 2.0.0 this is **optional**, not required. That release replaced
the Bitnami ClickHouse — which genuinely could not run Langfuse v4 — with the
same operator, so `langfuse.clickhouse.deploy=true` is now a valid way to run
v4. The difference is ownership: upstream's copy is part of the Helm release,
this one is not. Pick this one when you want the trace store to outlive the
release; pick upstream's when you would rather have one fewer moving part.

**2. Render-time guards.** Configurations known to break or lose data fail at
`helm template` time instead of reaching the cluster:

| Guard | Refuses |
|---|---|
| `requireExternalState` | any of postgresql / clickhouse / s3 still owned by the chart |
| *(always on)* | the image tag `latest` |

`blockV4WithBundledClickHouse` was dropped in chart 0.3.0. It refused a v4
image tag while `langfuse.clickhouse.deploy` was `true`, which was correct
while the upstream chart bundled Bitnami's ClickHouse. Upstream 2.0.0 replaced
that with the ClickHouse operator, which runs v4 — so the guard would now
refuse a valid configuration.

**3. Optional pre-upgrade backup check.** With
`preUpgradeBackupCheck.enabled=true`, a Helm `pre-upgrade` hook refuses the
upgrade unless a CNPG backup of the metadata database completed within
`maxAgeHours`. Off by default: it needs an image with both a shell and
`kubectl`, which the distroless `registry.k8s.io/kubectl` image is not.

## Values layout

Everything under `langfuse:` is passed to the upstream chart. Mind the double
nesting — `langfuse.langfuse.*` is upstream's own top-level `langfuse:` key,
while `langfuse.clickhouse.*` and friends are its sub-chart toggles:

```yaml
langfuse:
  langfuse:
    image:
      tag: "4.35.0"      # pin explicitly; "latest" is refused
  clickhouse:
    deploy: false        # external, operator-managed
```

Deployment-specific values (hostnames, resources, secret references) belong in
the deploying repository, not here. See
`flux-home/applications/langfuse/helm-values-configmap.yaml`.

## Why the metadata database matters

Langfuse computes cost at ingestion from a model-price table stored in
Postgres. A model with no price entry computes `$0` **silently** — nothing
errors, and re-ingesting later does not recompute stored observations. Several
current models have no built-in price in any Langfuse release, so those
definitions exist nowhere else. That database is not reconstructible from
ClickHouse or object storage, which is why the guards centre on it.

## Development

```bash
helm dependency build charts/langfuse-stack
charts/langfuse-stack/ci/render.sh lint
charts/langfuse-stack/ci/render.sh                      # helm template
charts/langfuse-stack/ci/render.sh --set langfuse.langfuse.image.tag=4.0.0
```

`ci/render.sh` generates the encryption key, salt and passwords the upstream
chart demands, so no credential-shaped literals are committed.
`ci/test-values.yaml` holds only the non-secret structure needed to reach the
guards; neither is a deployment example.

Do not add `charts/*.tgz` to `.helmignore` — Helm resolves the packaged
dependency from there, and ignoring it makes every render fail with
`found in Chart.yaml, but missing in charts/ directory`.
