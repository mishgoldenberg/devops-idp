{{/*
Shared pieces for the two backup CronJobs.

Both jobs talk to the same database, the same Artifactory repository and the same
secret, so their environment is defined once here. Copying it into both files is
how the dump ends up pointing at one repository and the restore test at another —
and that particular drift is invisible, because each job passes on its own.
*/}}

{{/* Image for the pg_dump / pg_restore phases. Defaults to the Postgres image so
     the client tools always match the server major version — a pg_dump older than
     the server refuses to run, and pg_restore across a major gap is a coin toss. */}}
{{- define "backup.pgImage" -}}
{{- .Values.postgres.backup.image | default .Values.postgres.image -}}
{{- end -}}

{{/* Image for every phase that speaks HTTP or writes backup_runs.

     This is the portal's own backend image, and it is not a convenience: the
     Postgres image in this cluster has no curl and no wget, so it physically
     cannot upload anything, and the backend image has no pg_dump, so it cannot
     produce anything to upload. Each phase runs where its tools are.

     The backend image is the right second image because it already exists in
     Artifactory, is already pullable with the existing pull secret, and carries
     python3 + psycopg2. Passed explicitly by the pipeline so the backup runs the
     SAME build as the pods do, rather than drifting onto :latest. */}}
{{- define "backup.pythonImage" -}}
{{- required "infrastructure.postgres.backup.pythonImage is empty while backups are enabled. It must be the backend image just built (the Postgres image has no HTTP client and cannot upload a dump). The pipeline sets it; for a manual render pass --set-string infrastructure.postgres.backup.pythonImage=<repo>/backend:<tag>." .Values.postgres.backup.pythonImage -}}
{{- end -}}

{{- define "backup.env" -}}
- name: PGHOST
  value: {{ .Values.postgres.serviceName | quote }}
- name: PGPORT
  value: {{ .Values.postgres.port | toString | quote }}
- name: PGUSER
  value: {{ .Values.postgres.postgresUser | quote }}
- name: PGDATABASE
  value: {{ .Values.postgres.postgresDB | quote }}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgres.secretName }}
      key: POSTGRES_PASSWORD
- name: PGCONNECT_TIMEOUT
  value: "10"
- name: PGSSLMODE
  value: "disable"
{{/* Only ever hashed, never stored or transmitted. The job needs it so the
     manifest can record WHICH secret the dump belongs to; see common.py. */}}
- name: JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgres.secretName }}
      key: JWT_SECRET
      optional: true
{{/* Same Artifactory host the portal's own widgets use — the pipeline passes
     ARTIFACTORY_BASE_URL straight through, so there is no second URL to keep in
     step with the first. It is already in the form the deploy API wants
     (https://host/artifactory); see scripts/artifactory_image_retention.py, which
     reads the same variable. */}}
- name: ART_BASE
  value: {{ required "infrastructure.postgres.backup.artifactory.baseUrl is empty while backups are enabled. It is fed from the ARTIFACTORY_BASE_URL pipeline variable. Set it, or set infrastructure.postgres.backup.enabled=false and accept that there is no backup." .Values.postgres.backup.artifactory.baseUrl | trimSuffix "/" | quote }}
- name: ART_REPO
  value: {{ required "infrastructure.postgres.backup.artifactory.repo is empty while backups are enabled." .Values.postgres.backup.artifactory.repo | quote }}
- name: ART_PATH
  value: {{ .Values.postgres.backup.artifactory.path | default "postgres" | quote }}
- name: ART_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgres.secretName }}
      key: ARTIFACTORY_BACKUP_USERNAME
- name: ART_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgres.secretName }}
      key: ARTIFACTORY_BACKUP_TOKEN
{{/* Where Artifactory presents an internal-CA certificate, the
     same reason global.integrationTlsVerify is false for the backend. */}}
- name: ART_TLS_VERIFY
  value: {{ .Values.postgres.backup.tlsVerify | toString | quote }}
- name: BACKUP_ENVIRONMENT
  value: {{ .Values.postgres.backup.environment | default "prod" | quote }}
- name: BACKUP_KEEP_LAST
  value: {{ required "global.backupKeepLast is not set." .Values.global.backupKeepLast | toString | quote }}
{{/* python:3.11-slim writes nothing to HOME, but pip/psycopg2 probe it and the
     root filesystem is read-only. */}}
- name: HOME
  value: /tmp
- name: PYTHONDONTWRITEBYTECODE
  value: "1"
- name: PYTHONUNBUFFERED
  value: "1"
{{/* Never let the container's locale pick the encoding. Several of these scripts
     print messages containing an em-dash, and a UnicodeEncodeError raised while
     REPORTING a failure would replace a clear diagnosis with a traceback. */}}
- name: PYTHONIOENCODING
  value: "utf-8"
{{- end -}}

{{- define "backup.securityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
readOnlyRootFilesystem: true
{{- end -}}

{{/* Requests AND limits on every container, including initContainers: the
     namespace ResourceQuota rejects any pod that omits them, and a CronJob whose
     pods are refused by quota fails silently at 01:15 rather than at deploy time. */}}
{{- define "backup.resources" -}}
requests:
  cpu: 100m
  memory: 192Mi
limits:
  cpu: 500m
  memory: 512Mi
{{- end -}}

{{- define "backup.volumes" -}}
- name: scripts
  configMap:
    name: postgres-backup-scripts
{{/* The hand-off between phases: the dump itself, the manifest, and the small
     status files each phase leaves for the next. sizeLimit is a real guard, not
     decoration — without it a database that has grown past the node's ephemeral
     storage evicts the job and, on a shared node, its neighbours. */}}
- name: work
  emptyDir:
    sizeLimit: {{ .Values.postgres.backup.workDirSize | default "4Gi" | quote }}
- name: tmp
  emptyDir:
    sizeLimit: 64Mi
{{- end -}}

{{- define "backup.volumeMounts" -}}
- name: scripts
  mountPath: /scripts
  readOnly: true
- name: work
  mountPath: /work
- name: tmp
  mountPath: /tmp
{{- end -}}
