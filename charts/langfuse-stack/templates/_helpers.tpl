{{/*
Expand the name of the chart.
*/}}
{{- define "langfuse-stack.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "langfuse-stack.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "langfuse-stack.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "langfuse-stack.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "langfuse-stack.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: langfuse
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
The Langfuse image tag actually in effect.
*/}}
{{- define "langfuse-stack.imageTag" -}}
{{- $lf := .Values.langfuse | default dict -}}
{{- $inner := index $lf "langfuse" | default dict -}}
{{- $image := index $inner "image" | default dict -}}
{{- index $image "tag" | default .Chart.AppVersion | toString -}}
{{- end }}

{{/*
Whether the upstream chart still owns a given stateful sub-chart.
*/}}
{{- define "langfuse-stack.subchartDeploys" -}}
{{- $lf := .Values.langfuse | default dict -}}
{{- $sub := index $lf (.subchart) | default dict -}}
{{- if hasKey $sub "deploy" -}}{{- index $sub "deploy" -}}{{- else -}}false{{- end -}}
{{- end }}

{{/*
Render-time assertions. Included from guards.yaml so they run on every
template, install and upgrade — a guard that only runs sometimes is not a
guard.
*/}}
{{- define "langfuse-stack.assertions" -}}
{{- $tag := include "langfuse-stack.imageTag" . -}}
{{- $ch := eq "true" (include "langfuse-stack.subchartDeploys" (dict "Values" .Values "subchart" "clickhouse")) -}}
{{- $pg := eq "true" (include "langfuse-stack.subchartDeploys" (dict "Values" .Values "subchart" "postgresql")) -}}
{{- $s3 := eq "true" (include "langfuse-stack.subchartDeploys" (dict "Values" .Values "subchart" "s3")) -}}

{{- if and .Values.guards.blockV4WithBundledClickHouse $ch -}}
{{- if or (hasPrefix "4." $tag) (eq $tag "4") -}}
{{- fail (printf "langfuse-stack: image tag %q is Langfuse v4, but langfuse.clickhouse.deploy is still true. The ClickHouse bundled with the upstream chart is not compatible with v4 — move ClickHouse to the operator (clickhouseCluster.enabled=true, langfuse.clickhouse.deploy=false) before changing the tag. Set guards.blockV4WithBundledClickHouse=false to override." $tag) -}}
{{- end -}}
{{- end -}}

{{- if .Values.guards.requireExternalState -}}
{{- $owned := list -}}
{{- if $ch }}{{- $owned = append $owned "clickhouse" -}}{{- end -}}
{{- if $pg }}{{- $owned = append $owned "postgresql" -}}{{- end -}}
{{- if $s3 }}{{- $owned = append $owned "s3" -}}{{- end -}}
{{- if $owned -}}
{{- fail (printf "langfuse-stack: guards.requireExternalState is on, but the chart still owns persistent state: %s. Point those at external instances (deploy=false) so a chart upgrade cannot touch the data." (join ", " $owned)) -}}
{{- end -}}
{{- end -}}

{{- if and (eq $tag "latest") true -}}
{{- fail "langfuse-stack: refusing to render with image tag \"latest\" — the running version must be reproducible, and the cluster's admission policy rejects it anyway." -}}
{{- end -}}
{{- end }}
