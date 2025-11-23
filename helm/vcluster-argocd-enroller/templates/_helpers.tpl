{{/*
Expand the name of the chart.
*/}}
{{- define "vcluster-argocd-enroller.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "vcluster-argocd-enroller.fullname" -}}
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
{{- define "vcluster-argocd-enroller.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "vcluster-argocd-enroller.labels" -}}
helm.sh/chart: {{ include "vcluster-argocd-enroller.chart" . }}
{{ include "vcluster-argocd-enroller.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.extraLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "vcluster-argocd-enroller.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vcluster-argocd-enroller.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "vcluster-argocd-enroller.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "vcluster-argocd-enroller.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Get the namespace for vCluster instances
*/}}
{{- define "vcluster-argocd-enroller.vclusterNamespace" -}}
{{- default "vcluster-system" .Values.operator.vclusterNamespace }}
{{- end }}

{{/*
Get the namespace for ArgoCD
*/}}
{{- define "vcluster-argocd-enroller.argoCDNamespace" -}}
{{- default "argocd" .Values.operator.argoCDNamespace }}
{{- end }}