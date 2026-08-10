{{/*
Expand the name of the chart.
*/}}
{{- define "publishhub.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
*/}}
{{- define "publishhub.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "publishhub.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to all resources.
*/}}
{{- define "publishhub.labels" -}}
helm.sh/chart: {{ include "publishhub.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ include "publishhub.name" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end }}

{{/*
Labels for a specific component. Call with a dict: include "publishhub.componentLabels" (dict "context" . "component" "api")
*/}}
{{- define "publishhub.componentLabels" -}}
{{ include "publishhub.labels" .context }}
app.kubernetes.io/name: {{ include "publishhub.name" .context }}-{{ .component }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/instance: {{ .context.Release.Name }}
{{- end }}

{{/*
Selector labels for a specific component. Call with a dict: include "publishhub.selectorLabels" (dict "context" . "component" "api")
*/}}
{{- define "publishhub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "publishhub.name" .context }}-{{ .component }}
app.kubernetes.io/instance: {{ .context.Release.Name }}
{{- end }}

{{/*
Component full name. Call with a dict: include "publishhub.componentFullname" (dict "context" . "component" "api")
*/}}
{{- define "publishhub.componentFullname" -}}
{{- printf "%s-%s" (include "publishhub.fullname" .context) .component }}
{{- end }}

{{/*
Image reference for a component. Call with a dict: include "publishhub.image" (dict "image" .Values.api.image "tag" .Values.global.image.tag)
The image object must have .repository and optionally .tag. If .tag is empty, falls back to the global tag.
*/}}
{{- define "publishhub.image" -}}
{{- $tag := .image.tag | default .tag | default "latest" -}}
{{- printf "%s:%s" .image.repository $tag }}
{{- end }}

{{/*
Service account name for the chart.
*/}}
{{- define "publishhub.serviceAccountName" -}}
{{- if .Values.serviceAccount.name }}
{{- .Values.serviceAccount.name }}
{{- else }}
{{- include "publishhub.fullname" . }}
{{- end }}
{{- end }}

{{/*
Redis address for KEDA trigger metadata.
Returns the REDIS_URL value suitable for KEDA's addressFromEnv.
*/}}
{{- define "publishhub.redisAddress" -}}
{{- .Values.redis.url }}
{{- end }}
