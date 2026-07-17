{{/*
Expand the name of the chart.
*/}}
{{- define "esphome-fleet-server.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Truncated at 63 chars (Kubernetes DNS-1123 limit).
Defaults to Release.Name; the chart name is long (esphome-fleet-server) so the
standard "<release>-<chart>" pattern produces ugly resource names. Users
running multiple releases in one namespace can disambiguate via
`fullnameOverride`.
*/}}
{{- define "esphome-fleet-server.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "esphome-fleet-server.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "esphome-fleet-server.labels" -}}
helm.sh/chart: {{ include "esphome-fleet-server.chart" . }}
{{ include "esphome-fleet-server.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "esphome-fleet-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "esphome-fleet-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "esphome-fleet-server.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "esphome-fleet-server.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Name of the Secret containing SERVER_TOKEN. Either chart-managed or user-supplied.
*/}}
{{- define "esphome-fleet-server.secretName" -}}
{{- if .Values.server.existingSecret -}}
{{- .Values.server.existingSecret -}}
{{- else -}}
{{- include "esphome-fleet-server.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "esphome-fleet-server.secretTokenKey" -}}
{{- if .Values.server.existingSecret -}}
{{- default "SERVER_TOKEN" .Values.server.existingSecretTokenKey -}}
{{- else -}}
SERVER_TOKEN
{{- end -}}
{{- end -}}
