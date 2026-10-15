{{- define "mcp-admin.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mcp-admin.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "mcp-admin.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Common labels; add app.kubernetes.io/component per workload. */}}
{{- define "mcp-admin.labels" -}}
helm.sh/chart: {{ include "mcp-admin.chart" . }}
app.kubernetes.io/name: {{ include "mcp-admin.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: mcp-gateway
{{- end }}

{{/* Selector labels for one component: (dict "ctx" . "component" "api") */}}
{{- define "mcp-admin.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-admin.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "mcp-admin.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mcp-admin.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Image ref for a component: (dict "ctx" . "image" .Values.api.image) */}}
{{- define "mcp-admin.image" -}}
{{- if .image.digest }}
{{- printf "%s@%s" .image.repository .image.digest }}
{{- else }}
{{- printf "%s:%s" .image.repository (default .ctx.Chart.AppVersion .image.tag) }}
{{- end }}
{{- end }}

{{- define "mcp-admin.envSecretName" -}}
{{- default (printf "%s-api-env" (include "mcp-admin.fullname" .)) .Values.secrets.env.name }}
{{- end }}
