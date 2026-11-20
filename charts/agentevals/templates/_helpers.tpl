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
{{- $registry := (.Values.global | default dict).imageRegistry | default .Values.image.registry | default .Values.registry -}}
{{- $tag := .Values.image.tag | default .Values.tag | default .Chart.AppVersion -}}
{{- $ref := printf "%s:%s" .Values.image.repository $tag -}}
{{- if .Values.image.digest -}}
{{- $ref = printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- end -}}
{{- if $registry -}}
{{- printf "%s/%s" $registry $ref -}}
{{- else -}}
{{- $ref -}}
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
{{- $registry := (.Values.global | default dict).imageRegistry | default $pg.image.registry -}}
{{- printf "%s/%s/%s:%s" $registry $pg.image.repository $pg.image.name $pg.image.tag -}}
{{- end -}}

{{/*
imagePullSecrets for every pod: imagePullSecrets merged (union) with global.imagePullSecrets.
*/}}
{{- define "agentevals.imagePullSecrets" -}}
{{- with concat (.Values.imagePullSecrets | default list) ((.Values.global | default dict).imagePullSecrets | default list) | uniq -}}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end -}}
{{- end -}}

{{/*
imagePullPolicy for a container: its own value, then imagePullPolicy, then global.imagePullPolicy,
then IfNotPresent.
Usage: {{ include "agentevals.imagePullPolicy" (dict "root" $ "local" .Values.image.pullPolicy) }}
*/}}
{{- define "agentevals.imagePullPolicy" -}}
{{- .local | default .root.Values.imagePullPolicy | default ((.root.Values.global | default dict).imagePullPolicy) | default "IfNotPresent" -}}
{{- end -}}

{{/*
ClusterRole and ClusterRoleBinding name for TokenReviews; cluster scoped, so it carries the namespace.
*/}}
{{- define "agentevals.tokenReviewName" -}}
{{- printf "%s-%s-tokenreview" (include "agentevals.namespace" .) (include "agentevals.fullname" .) -}}
{{- end -}}

{{/*
grpcApi.auth.allowedServiceAccounts as a JSON list, each entry rendered with tpl, so a parent chart
can name its own namespace and release in an entry.
*/}}
{{- define "agentevals.allowedServiceAccounts" -}}
{{- $rendered := list -}}
{{- range .Values.grpcApi.auth.allowedServiceAccounts -}}
{{- $rendered = append $rendered (tpl (toString .) $ | trim) -}}
{{- end -}}
{{- toJson $rendered -}}
{{- end -}}

{{/*
Fail the render on a gRPC API setup the server would refuse, or one that grants the TokenReview
permission to the namespace's default ServiceAccount.
*/}}
{{- define "agentevals.validateGrpcApi" -}}
{{- $api := .Values.grpcApi -}}
{{- if and $api.only (not $api.enabled) -}}
{{- fail "grpcApi.only needs grpcApi.enabled=true" -}}
{{- end -}}
{{- if $api.auth.enabled -}}
{{- if not $api.enabled -}}
{{- fail "grpcApi.auth.enabled needs grpcApi.enabled=true" -}}
{{- end -}}
{{- if empty $api.auth.audience -}}
{{- fail "grpcApi.auth.enabled needs grpcApi.auth.audience" -}}
{{- end -}}
{{- if empty $api.auth.allowedServiceAccounts -}}
{{- fail "grpcApi.auth.enabled needs grpcApi.auth.allowedServiceAccounts (namespace/name): any pod can mint a token for any audience, so the audience alone authorizes nothing" -}}
{{- end -}}
{{- range include "agentevals.allowedServiceAccounts" . | fromJsonArray -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?/[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$" .) -}}
{{- fail (printf "grpcApi.auth.allowedServiceAccounts: %q is not namespace/name" .) -}}
{{- end -}}
{{- end -}}
{{- if and (not .Values.serviceAccount.create) (empty .Values.serviceAccount.name) -}}
{{- fail "grpcApi.auth.enabled needs serviceAccount.create=true or serviceAccount.name, so the TokenReview permission does not go to the namespace's default ServiceAccount" -}}
{{- end -}}
{{- if and .Values.serviceAccount.create (not .Values.serviceAccount.automount) -}}
{{- fail "grpcApi.auth.enabled needs serviceAccount.automount=true: the server calls the TokenReview API with its ServiceAccount token" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Secret name holding POSTGRES_PASSWORD for the bundled Postgres instance.
*/}}
{{- define "agentevals.passwordSecretName" -}}
{{- printf "%s-postgresql" (include "agentevals.fullname" .) -}}
{{- end -}}
