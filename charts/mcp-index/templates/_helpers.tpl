{{- define "mcp-index.labels" -}}
app.kubernetes.io/name: mcp-index
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: mcp-toolsets
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "mcp-index.selectorLabels" -}}
app.kubernetes.io/name: mcp-index
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mcp-index.host" -}}
{{- required "ingress.host is required (--set ingress.host=<shared-domain>)" .Values.ingress.host -}}
{{- end }}
