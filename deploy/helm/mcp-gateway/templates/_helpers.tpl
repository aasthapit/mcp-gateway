{{/*
Expand the name of the chart.
*/}}
{{- define "mcp-gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name (release-name aware, 63 chars max).
*/}}
{{- define "mcp-gateway.fullname" -}}
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

{{- define "mcp-gateway.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Standard labels (app.kubernetes.io/*) applied to every object.
*/}}
{{- define "mcp-gateway.labels" -}}
helm.sh/chart: {{ include "mcp-gateway.chart" . }}
{{ include "mcp-gateway.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: mcp-gateway
{{- end }}

{{/*
Selector labels: immutable once the Deployment exists, so keep them minimal.
*/}}
{{- define "mcp-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: gateway
{{- end }}

{{- define "mcp-gateway.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mcp-gateway.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference: repository@digest wins over repository:tag; tag defaults to appVersion.
*/}}
{{- define "mcp-gateway.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
{{- end }}

{{- define "mcp-gateway.envSecretName" -}}
{{- default (printf "%s-env" (include "mcp-gateway.fullname" .)) .Values.secrets.env.name }}
{{- end }}

{{- define "mcp-gateway.upstreamsSecretName" -}}
{{- default (printf "%s-upstreams" (include "mcp-gateway.fullname" .)) .Values.secrets.upstreams.name }}
{{- end }}

{{/*
External origin: explicit config.PUBLIC_BASE_URL, else https://<route.host>.
*/}}
{{- define "mcp-gateway.publicBaseUrl" -}}
{{- if .Values.config.PUBLIC_BASE_URL }}
{{- .Values.config.PUBLIC_BASE_URL }}
{{- else if .Values.route.host }}
{{- printf "https://%s" .Values.route.host }}
{{- else if eq (toString .Values.config.ENV) "prod" }}
{{- fail "set route.host or config.PUBLIC_BASE_URL (GATEWAY_PUBLIC_BASE_URL must be https:// in prod)" }}
{{- else }}
{{- printf "http://%s:%d" (include "mcp-gateway.fullname" .) (int .Values.service.port) }}
{{- end }}
{{- end }}

{{/*
Allowed Host headers: explicit config.ALLOWED_HOSTS (JSON list), else the route host plus the
in-cluster Service names (probes and admin-api use those).
*/}}
{{- define "mcp-gateway.allowedHosts" -}}
{{- if .Values.config.ALLOWED_HOSTS }}
{{- .Values.config.ALLOWED_HOSTS }}
{{- else }}
{{- $svc := include "mcp-gateway.fullname" . }}
{{- $hosts := list (printf "%s:%d" $svc (int .Values.service.port)) (printf "%s.%s.svc:%d" $svc .Release.Namespace (int .Values.service.port)) (printf "%s.%s.svc.cluster.local:%d" $svc .Release.Namespace (int .Values.service.port)) }}
{{- if .Values.route.host }}
{{- $hosts = prepend $hosts .Values.route.host }}
{{- end }}
{{- $hosts | toJson }}
{{- end }}
{{- end }}
