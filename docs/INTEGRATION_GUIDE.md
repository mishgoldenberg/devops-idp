# Integration Guide

This guide explains how to replace mock adapters with real integrations for external systems.

---

## General Integration Pattern

All external system integrations follow the **Adapter Pattern**:

```
Service
├── src/
│   ├── adapters/
│   │   ├── mock-adapter.ts    # Development mock
│   │   ├── real-adapter.ts    # Production integration
│   │   └── index.ts           # Adapter factory
│   ├── config/
│   │   └── index.ts           # Configuration
│   └── index.ts               # Service entry point
```

To switch from mock to real integration:

1. Set environment variable: `USE_MOCK_DATA=false`
2. Configure credentials in environment or Vault
3. Implement real adapter methods
4. Test connectivity

---

## Azure DevOps Integration

### Configuration

```bash
# Environment variables
USE_MOCK_AZURE_DEVOPS=false
AZURE_DEVOPS_ORG=your-organization
AZURE_DEVOPS_PAT=your-personal-access-token
AZURE_DEVOPS_API_URL=https://dev.azure.com/${AZURE_DEVOPS_ORG}
```

### Implementation

File: `backend/services/azure-devops-service/src/adapters/real-adapter.ts`

```typescript
export class RealAzureDevOpsAdapter {
  private client: AxiosInstance;

  constructor(org: string, pat: string) {
    this.client = axios.create({
      baseURL: `https://dev.azure.com/${org}`,
      headers: {
        'Authorization': `Basic ${Buffer.from(`:${pat}`).toString('base64')}`,
      },
    });
  }

  async getWorkItems(username: string): Promise<WorkItem[]> {
    const response = await this.client.post(
      `/_apis/wit/wiql?api-version=7.0`,
      {
        query: `SELECT [System.Id], [System.Title], [System.State] 
                FROM WorkItems 
                WHERE [System.AssignedTo] = '${username}'
                AND [System.State] <> 'Closed'`
      }
    );
    
    const workItemIds = response.data.workItems.map((wi: any) => wi.id);
    
    // Batch fetch work item details
    const detailsResponse = await this.client.post(
      `/_apis/wit/workitemsbatch?api-version=7.0`,
      {
        ids: workItemIds,
        fields: ['System.Id', 'System.Title', 'System.State', 'System.WorkItemType']
      }
    );
    
    return detailsResponse.data.value.map((item: any) => ({
      id: item.id,
      title: item.fields['System.Title'],
      state: item.fields['System.State'],
      type: item.fields['System.WorkItemType'],
      // ... map other fields
    }));
  }
}
```

### API Documentation

- [Azure DevOps REST API Reference](https://learn.microsoft.com/en-us/rest/api/azure/devops/)
- [Work Item Tracking API](https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/)

---

## SonarQube Integration

### Configuration

```bash
USE_MOCK_SONARQUBE=false
SONARQUBE_URL=https://sonarqube.internal.company
SONARQUBE_TOKEN=your-sonarqube-token
```

### Implementation

```typescript
export class RealSonarQubeAdapter {
  private client: AxiosInstance;

  constructor(url: string, token: string) {
    this.client = axios.create({
      baseURL: url,
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    });
  }

  async getProjects(): Promise<SonarProject[]> {
    const response = await this.client.get('/api/components/search', {
      params: {
        qualifiers: 'TRK',
        ps: 100
      }
    });
    
    // For each project, fetch quality gate and metrics
    const projects = await Promise.all(
      response.data.components.map(async (component: any) => {
        const [qualityGate, measures] = await Promise.all([
          this.getQualityGate(component.key),
          this.getMetrics(component.key)
        ]);
        
        return {
          key: component.key,
          name: component.name,
          quality_gate: qualityGate,
          metrics: measures
        };
      })
    );
    
    return projects;
  }
}
```

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

```typescript
export class RealArtifactoryAdapter {
  private client: AxiosInstance;

  constructor(url: string, apiKey: string, username: string) {
    this.client = axios.create({
      baseURL: url,
      headers: {
        'X-JFrog-Art-Api': apiKey,
      },
      auth: {
        username,
        password: apiKey
      }
    });
  }

  async getArtifacts(): Promise<Artifact[]> {
    const response = await this.client.get('/api/storage/', {
      params: {
        list: true,
        deep: 1,
        listFolders: 0
      }
    });
    
    return response.data.files.map((file: any) => ({
      name: file.uri,
      path: file.path,
      size: file.size,
      created: new Date(file.created),
      // ... map other fields
    }));
  }
}
```

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

```typescript
export class RealServiceNowAdapter {
  private client: AxiosInstance;
  private accessToken: string | null = null;

  constructor(url: string, clientId: string, clientSecret: string) {
    this.client = axios.create({
      baseURL: url,
    });
  }

  async authenticate(): Promise<void> {
    const response = await this.client.post('/oauth_token.do', {
      grant_type: 'client_credentials',
      client_id: this.clientId,
      client_secret: this.clientSecret
    });
    
    this.accessToken = response.data.access_token;
  }

  async getTickets(username: string): Promise<Ticket[]> {
    if (!this.accessToken) {
      await this.authenticate();
    }
    
    const response = await this.client.get('/api/now/table/incident', {
      headers: {
        'Authorization': `Bearer ${this.accessToken}`
      },
      params: {
        sysparm_query: `assigned_to.email=${username}^active=true`,
        sysparm_fields: 'number,short_description,state,priority,sys_created_on',
        sysparm_limit: 100
      }
    });
    
    return response.data.result.map((incident: any) => ({
      number: incident.number,
      short_description: incident.short_description,
      state: incident.state,
      priority: incident.priority,
      // ... map other fields
    }));
  }
}
```

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

```typescript
export class RealAIChatbotAdapter {
  private client: AxiosInstance;

  constructor(apiUrl: string, token: string) {
    this.client = axios.create({
      baseURL: apiUrl,
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    });
  }

  async sendMessage(message: string, conversationId?: string): Promise<ChatResponse> {
    const response = await this.client.post('/chat', {
      message,
      conversation_id: conversationId,
      stream: false
    });
    
    return {
      message: response.data.response,
      conversationId: response.data.conversation_id,
      // ... map other fields
    };
  }
}
```

---

## Testing Integrations

### 1. Unit Tests

```typescript
// Example test
describe('RealAzureDevOpsAdapter', () => {
  it('should fetch work items', async () => {
    const adapter = new RealAzureDevOpsAdapter('org', 'pat');
    const workItems = await adapter.getWorkItems('user@email.com');
    
    expect(workItems).toBeInstanceOf(Array);
    expect(workItems[0]).toHaveProperty('id');
    expect(workItems[0]).toHaveProperty('title');
  });
});
```

### 2. Integration Tests

```bash
# Set test environment
export USE_MOCK_DATA=false
export AZURE_DEVOPS_ORG=test-org
export AZURE_DEVOPS_PAT=test-pat

# Run integration tests
npm run test:integration
```

### 3. Manual Testing

```bash
# Start service
cd backend/services/azure-devops-service
npm run dev

# Test endpoint
curl -X GET "http://localhost:8002/work-items?username=test@email.com"
```

---

## Error Handling

All adapters should implement proper error handling:

```typescript
async getWorkItems(username: string): Promise<WorkItem[]> {
  try {
    const response = await this.client.get('/api/workitems');
    return response.data;
  } catch (error) {
    if (axios.isAxiosError(error)) {
      if (error.response?.status === 401) {
        throw new Error('Authentication failed - check credentials');
      } else if (error.response?.status === 404) {
        throw new Error('Resource not found');
      } else if (error.response?.status >= 500) {
        throw new Error('External service unavailable');
      }
    }
    throw new Error(`Failed to fetch work items: ${error.message}`);
  }
}
```

---

## Caching Strategy

Implement caching to reduce external API calls:

```typescript
async getWorkItems(username: string): Promise<WorkItem[]> {
  const cacheKey = `workitems:${username}`;
  
  // Check cache
  const cached = await this.redis.get(cacheKey);
  if (cached) {
    return JSON.parse(cached);
  }
  
  // Fetch from API
  const workItems = await this.fetchFromAPI(username);
  
  // Cache for 5 minutes
  await this.redis.setex(cacheKey, 300, JSON.stringify(workItems));
  
  return workItems;
}
```

---

## Monitoring Integration Health

Update service health status:

```typescript
async updateHealthStatus(serviceName: string, isHealthy: boolean) {
  const status = isHealthy ? 'HEALTHY' : 'DOWN';
  
  await pool.query(
    `UPDATE service_health 
     SET status = $1, last_check_at = CURRENT_TIMESTAMP 
     WHERE service_name = $2`,
    [status, serviceName]
  );
}
```

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

