{{- define "opsdeck.name" -}}{{ if contains "opsdeck" .Release.Name }}{{ .Release.Name }}{{ else }}{{ .Release.Name }}-opsdeck{{ end }}{{- end -}}
{{- define "opsdeck.labels" -}}
app.kubernetes.io/name: opsdeck
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}
{{- define "opsdeck.selector" -}}
app.kubernetes.io/name: opsdeck
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
