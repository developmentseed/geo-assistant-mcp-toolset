{{- define "mcp-chat.labels" -}}
app.kubernetes.io/name: mcp-chat
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: mcp-toolsets
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "mcp-chat.selectorLabels" -}}
app.kubernetes.io/name: mcp-chat
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mcp-chat.host" -}}
{{- required "ingress.host is required (--set ingress.host=chat.<shared-domain>)" .Values.ingress.host -}}
{{- end }}
