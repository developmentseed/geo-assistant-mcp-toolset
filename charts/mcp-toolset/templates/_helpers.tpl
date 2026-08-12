{{- define "mcp-toolset.name" -}}
{{- required "name is required (--set name=<toolset-name>)" .Values.name -}}
{{- end }}

{{- define "mcp-toolset.fullname" -}}
mcp-{{ include "mcp-toolset.name" . }}
{{- end }}

{{- define "mcp-toolset.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-toolset.fullname" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mcp-toolset.labels" -}}
{{ include "mcp-toolset.selectorLabels" . }}
app.kubernetes.io/part-of: mcp-toolsets
app.kubernetes.io/managed-by: {{ .Release.Service }}
mcp-toolsets/toolset: {{ include "mcp-toolset.name" . }}
{{- end }}
