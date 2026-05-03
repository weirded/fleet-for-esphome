{{/*
Expand the name of the chart.
*/}}
{{- define "esphome-fleet-worker.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Truncated at 63 chars (Kubernetes DNS-1123 limit).
*/}}
{{- define "esphome-fleet-worker.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "esphome-fleet-worker.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "esphome-fleet-worker.labels" -}}
helm.sh/chart: {{ include "esphome-fleet-worker.chart" . }}
{{ include "esphome-fleet-worker.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "esphome-fleet-worker.selectorLabels" -}}
app.kubernetes.io/name: {{ include "esphome-fleet-worker.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "esphome-fleet-worker.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "esphome-fleet-worker.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Name of the Secret containing SERVER_TOKEN. Either chart-managed or user-supplied.
*/}}
{{- define "esphome-fleet-worker.secretName" -}}
{{- if .Values.server.existingSecret -}}
{{- .Values.server.existingSecret -}}
{{- else -}}
{{- include "esphome-fleet-worker.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "esphome-fleet-worker.secretTokenKey" -}}
{{- if .Values.server.existingSecret -}}
{{- default "SERVER_TOKEN" .Values.server.existingSecretTokenKey -}}
{{- else -}}
SERVER_TOKEN
{{- end -}}
{{- end -}}
