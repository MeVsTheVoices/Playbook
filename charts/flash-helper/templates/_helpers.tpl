{{- define "flash-helper.name" -}}
{{- printf "%s-flasher" .Release.Name | trunc 52 | trimSuffix "-" -}}
{{- end -}}

{{- define "flash-helper.job" -}}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "flash-helper.name" . }}
  namespace: {{ .Release.Namespace }}
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app.kubernetes.io/name: playbook-flash-helper
        app.kubernetes.io/instance: {{ .Release.Name }}
    spec:
      nodeName: {{ .Values.nodeName | quote }}
      automountServiceAccountToken: false
      restartPolicy: Never
      terminationGracePeriodSeconds: 86400
      containers:
        - name: flasher
          image: {{ printf "%s:%s" .Values.image.repository .Values.image.tag | quote }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          securityContext:
            privileged: true
            runAsUser: 0
          env:
            - name: FLASH_TOKEN_FILE
              value: /token/token
            - name: FLASH_ALLOWED_DEVICES
              value: {{ join "," .Values.allowedDevices | quote }}
            - name: FLASH_IDLE_SECONDS
              value: {{ .Values.idleSeconds | quote }}
            - name: FLASH_HOST_MOUNTINFO
              value: /host/mountinfo
          ports:
            - name: helper
              containerPort: 8090
          readinessProbe:
            httpGet:
              path: /health
              port: helper
            periodSeconds: 2
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          volumeMounts:
            - name: devices
              mountPath: /dev
            - name: mountinfo
              mountPath: /host/mountinfo
              readOnly: true
            - name: token
              mountPath: /token
              readOnly: true
            - name: work
              mountPath: /work
            - name: data
              mountPath: /data
      volumes:
        - name: devices
          hostPath:
            path: /dev
            type: Directory
        - name: mountinfo
          hostPath:
            path: /proc/1/mountinfo
            type: File
        - name: token
          secret:
            secretName: {{ .Values.existingSecret | quote }}
            defaultMode: 0400
            items:
              - key: {{ .Values.tokenKey | quote }}
                path: token
        - name: work
          emptyDir:
            sizeLimit: {{ .Values.workSize }}
        - name: data
          emptyDir:
            sizeLimit: {{ .Values.workSize }}
{{- end -}}
