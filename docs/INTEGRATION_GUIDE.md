# Integration Guide

This guide explains how to replace mock adapters with real integrations for external systems.

---

## General Integration Pattern

All external system integrations now follow the **Adapter Pattern inside the Python backend**:

```
backend/python_backend/app/
├── api/
│   ├── azure_devops.py   # ADO adapter (mocked by default)
│   ├── sonarqube.py      # SonarQube adapter (mocked by default)
│   ├── artifactory.py    # Artifactory adapter (mocked by default)
│   ├── servicenow.py     # ServiceNow adapter (mocked by default)
│   └── ai_chatbot.py     # AI chatbot adapter (mocked by default)
└── ...
```

To switch from mock to real integration:

1. Add real HTTP calls in the relevant `*.py` module (e.g. `azure_devops.py`)
2. Configure credentials in environment variables or Vault
3. Keep the response JSON shape compatible with the existing API reference
4. Test connectivity from the running Python backend container

---

## Azure DevOps Integration

> **Full documentation:** [docs/AZURE_DEVOPS.md](./AZURE_DEVOPS.md)

### Quick-start configuration

```bash
USE_MOCK_AZURE_DEVOPS=false
AZURE_DEVOPS_ORG=YourOrgName          # organisation name only, NOT the full URL
AZURE_DEVOPS_PAT=your-pat-here        # server-level fallback for the admin account
```

### Authentication model

- Each user saves their own PAT via the "Connect via PAT" prompt in the Azure DevOps widgets.
- PATs are stored server-side in the `system_config` PostgreSQL table (`is_sensitive = true`), never returned to the browser or written to logs.
- If `AZURE_DEVOPS_PAT` is set in the environment, it acts as a fallback for users who have not yet configured a personal PAT (primarily the admin account).
- Vault storage is supported — set `USE_VAULT=true` (see full docs for details).

### What's implemented

| Feature | Location |
|---|---|
| Per-user PAT storage (DB or Vault) | `backend/app/secrets_manager.py` |
| PAT management endpoints (GET / POST / DELETE) | `backend/app/api/azure_devops.py` |
| Work items — `@Me` WIQL, project filter, state categories | `backend/app/api/azure_devops.py` |
| Project list for widget dropdown | `backend/app/api/azure_devops.py` |
| Shared connection hook with 60 s cache | `frontend/src/hooks/useAzureDevOpsConnection.ts` |
| Reusable connect/disconnect UI | `frontend/src/components/common/AzureConnectPrompt.tsx` |
| Work items widget with project dropdown | `frontend/src/components/widgets/AzureDevOpsWorkItems.tsx` |
| PR and Pipeline widgets | `frontend/src/components/widgets/Latest{PR,Pipeline}Widget.tsx` |

### API Documentation

- [Azure DevOps REST API Reference](https://learn.microsoft.com/en-us/rest/api/azure/devops/)
- [Work Item Tracking API](https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/)
- [Personal Access Tokens](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate)

---

## SonarQube Integration

### Configuration

```bash
USE_MOCK_SONARQUBE=false
SONARQUBE_URL=https://sonarqube.internal.company
SONARQUBE_TOKEN=your-sonarqube-token
```

### Implementation

Extend `backend/python_backend/app/api/sonarqube.py` to call the real SonarQube API
and keep the response structure aligned with the existing widgets.

### API Documentation

- [SonarQube Web API](https://docs.sonarqube.org/latest/extend/web-api/)

---

## Artifactory Integration

### Configuration

```bash
USE_MOCK_ARTIFACTORY=false
ARTIFACTORY_URL=https://artifactory.internal.company
ARTIFACTORY_API_KEY=your-api-key
ARTIFACTORY_USERNAME=your-username
```

### Implementation

Extend `backend/python_backend/app/api/artifactory.py` with real HTTP calls
to Artifactory, mapping responses into the current JSON fields.

### API Documentation

- [Artifactory REST API](https://www.jfrog.com/confluence/display/JFROG/Artifactory+REST+API)

---

## ServiceNow Integration

### Configuration

```bash
USE_MOCK_SERVICENOW=false
SERVICENOW_INSTANCE=your-instance
SERVICENOW_URL=https://${SERVICENOW_INSTANCE}.service-now.com
SERVICENOW_CLIENT_ID=your-client-id
SERVICENOW_CLIENT_SECRET=your-client-secret
# OR
SERVICENOW_USERNAME=your-username
SERVICENOW_PASSWORD=your-password
```

### Implementation

Extend `backend/python_backend/app/api/servicenow.py` to call the real ServiceNow
APIs and return the same ticket JSON currently expected by the frontend.

### API Documentation

- [ServiceNow REST API](https://developer.servicenow.com/dev.do#!/reference/api/tokyo/rest)

---

## AI Chatbot Integration

### Configuration

```bash
USE_MOCK_AI_CHATBOT=false
AI_CHATBOT_API_URL=https://ai.internal.company/api
AI_CHATBOT_UI_URL=https://ai.internal.company
AI_CHATBOT_TOKEN=your-ai-token
```

### Implementation

Extend `backend/python_backend/app/api/ai_chatbot.py` to forward chat messages
to a real AI backend and return the response in the existing format.

---

## Testing Integrations

Use Python testing tools (e.g. `pytest`, FastAPI's `TestClient`) to exercise the
FastAPI endpoints once real integrations are added, and manually test via `curl`
against the `/api/...` endpoints exposed by the Python backend.

---

## Error Handling

All adapters should implement proper error handling, mapping external API
errors into clear messages and appropriate HTTP status codes from the
FastAPI routes.

---

## Caching Strategy

Implement caching to reduce external API calls using the shared Redis client
in `backend/python_backend/app/redis_client.py`.

---

## Monitoring Integration Health

Update service health status from the Python backend by writing to the
`service_health` table using the helpers in `backend/python_backend/app/db.py`.

---

## Troubleshooting

### Connection Issues

```bash
# Test network connectivity
curl -v https://dev.azure.com

# Test authentication
curl -u :$AZURE_DEVOPS_PAT https://dev.azure.com/{org}/_apis/projects
```

### Authentication Errors

- Verify credentials are correctly set
- Check token/key expiration
- Ensure proper permissions/scopes

### Rate Limiting

- Implement exponential backoff
- Add request queuing
- Monitor rate limit headers

---

## Security Best Practices

1. **Never commit credentials** - Use environment variables or Vault
2. **Rotate credentials regularly** - Implement key rotation
3. **Use least privilege** - Grant minimum required permissions
4. **Audit API usage** - Log all external API calls
5. **Encrypt in transit** - Always use HTTPS
6. **Validate responses** - Don't trust external data blindly

