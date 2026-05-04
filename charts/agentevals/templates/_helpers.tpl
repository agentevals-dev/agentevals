{{- define "agentevals.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agentevals.fullname" -}}
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

{{- define "agentevals.namespace" -}}
{{- default .Release.Namespace .Values.namespaceOverride }}
{{- end }}

{{- define "agentevals.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agentevals.image" -}}
{{- $registry := .Values.image.registry | default .Values.registry -}}
{{- $tag := .Values.image.tag | default .Values.tag | default .Chart.AppVersion -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .Values.image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end }}

{{- define "agentevals.labels" -}}
helm.sh/chart: {{ include "agentevals.chart" . }}
{{ include "agentevals.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: agentevals
{{- end }}

{{- define "agentevals.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agentevals.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- /*
Selector labels scoped to the main app Pod and its Service. Carries the
``app.kubernetes.io/component: agentevals`` discriminator so the agentevals
Service does not also match the bundled Postgres Pod (which carries
``app.kubernetes.io/component: database`` instead).
*/ -}}
{{- define "agentevals.app.selectorLabels" -}}
{{ include "agentevals.selectorLabels" . }}
app.kubernetes.io/component: agentevals
{{- end }}

{{- define "agentevals.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "agentevals.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Service name for the bundled Postgres instance.
*/}}
{{- define "agentevals.postgresqlServiceName" -}}
{{- printf "%s-postgresql" (include "agentevals.fullname" .) -}}
{{- end -}}

{{/*
Bundled Postgres image reference (registry/repository/name:tag).
*/}}
{{- define "agentevals.postgresql.image" -}}
{{- $pg := .Values.database.postgres.bundled -}}
{{- printf "%s/%s/%s:%s" $pg.image.registry $pg.image.repository $pg.image.name $pg.image.tag -}}
{{- end -}}

{{/*
Secret name holding POSTGRES_PASSWORD for the bundled Postgres instance.
*/}}
{{- define "agentevals.passwordSecretName" -}}
{{- printf "%s-postgresql" (include "agentevals.fullname" .) -}}
{{- end -}}
