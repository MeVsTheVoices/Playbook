{{- define "playbook.fullname" -}}
{{- if contains "playbook" .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-playbook" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "playbook.selectorLabels" -}}
app: playbook-callback-site
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "playbook.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "playbook.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "playbook.kubernetes" -}}
{{- if has "kubernetes" .Values.modules }}true{{ end -}}
{{- end -}}
