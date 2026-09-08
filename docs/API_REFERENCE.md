# API Reference

<!-- GENERATED:API — do not edit by hand; run scripts/gen_docs.py -->

193 endpoints across 26 groups, generated from the running application's OpenAPI schema.

Everything under `/api/…` returns JSON. Everything under `/ui/…` returns HTML fragments for HTMX and is not part of this reference — those are not an API, they are the pages.

Authentication is the `auth_token` cookie (HS256 JWT) on every route except `/api/health/*` and the sign-in routes. Admin-only routes are marked in their own handlers; a non-admin gets 403, never a filtered result.

## activity

| Method | Path | What it does |
| --- | --- | --- |
| `DELETE` | `/api/activity` | Clear Activity |
| `GET` | `/api/activity` | List Activity |

## admin

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/admin/dashboard-widgets` | List Dashboard Widgets |
| `PUT` | `/api/admin/dashboard-widgets` | Set Dashboard Widget Visibility |
| `POST` | `/api/admin/grant-role` | Grant Role |
| `POST` | `/api/admin/quick-links` | Create Quick Link |
| `DELETE` | `/api/admin/quick-links/{quick_link_id}` | Delete Quick Link |
| `PATCH` | `/api/admin/quick-links/{quick_link_id}` | Update Quick Link |
| `GET` | `/api/admin/roles` | List Roles |
| `GET` | `/api/admin/sso/config` | Get Sso Config |
| `POST` | `/api/admin/sso/config` | Save Admin Sso Config |
| `POST` | `/api/admin/sso/test` | Test Sso Connection |
| `GET` | `/api/admin/users` | List Users |
| `PUT` | `/api/admin/users/active` | Set User Active |
| `PUT` | `/api/admin/users/role` | Set User Role |

## announcements

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/announcements` | List Announcements |
| `POST` | `/api/announcements` | Create Announcement |
| `GET` | `/api/announcements/active` | Active Announcements |
| `DELETE` | `/api/announcements/{announcement_id}` | Delete Announcement |
| `PATCH` | `/api/announcements/{announcement_id}` | Update Announcement |
| `POST` | `/api/announcements/{announcement_id}/dismiss` | Dismiss |

## approvals

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/approvals/requests` | Get Requests |
| `POST` | `/api/approvals/requests` | Create Request |
| `DELETE` | `/api/approvals/requests/{request_id}` | Delete Request |
| `GET` | `/api/approvals/requests/{request_id}` | Get Request |
| `POST` | `/api/approvals/requests/{request_id}/approve` | Approve Request |
| `POST` | `/api/approvals/requests/{request_id}/force-fail` | Force Fail Request |
| `POST` | `/api/approvals/requests/{request_id}/reject` | Reject Request |
| `POST` | `/api/approvals/requests/{request_id}/servicenow-retry` | Retry Servicenow |

## audit-logs

| Method | Path | What it does |
| --- | --- | --- |
| `DELETE` | `/api/audit-logs` | Clear Audit Events |
| `GET` | `/api/audit-logs` | List Audit Events |
| `GET` | `/api/audit-logs/actions` | List Actions |
| `GET` | `/api/audit-logs/export` | Export Audit Events |
| `GET` | `/api/audit-logs/levels` | List Levels |

## auth

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/auth/login` | Oidc Login |
| `GET` | `/api/auth/sso/callback` | Sso Callback |

## azure-devops

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/azure-devops/area-paths` | Get Area Paths |
| `GET` | `/api/azure-devops/collections` | Get Collections |
| `GET` | `/api/azure-devops/iterations` | Get Iterations |
| `DELETE` | `/api/azure-devops/pat` | Remove Pat |
| `GET` | `/api/azure-devops/pat` | Get Pat Status |
| `POST` | `/api/azure-devops/pat` | Save Pat |
| `GET` | `/api/azure-devops/pipelines` | Get Pipelines |
| `GET` | `/api/azure-devops/projects` | Get Projects |
| `GET` | `/api/azure-devops/provisioning/collections` | Provisioning Collections |
| `GET` | `/api/azure-devops/provisioning/name-check` | Provisioning Name Check |
| `GET` | `/api/azure-devops/pull-requests` | Get Pull Requests |
| `GET` | `/api/azure-devops/repositories` | Get Repositories |
| `GET` | `/api/azure-devops/work-item-types` | Get Work Item Types |
| `GET` | `/api/azure-devops/work-items` | Get Work Items |
| `GET` | `/api/azure-devops/workitem-tasks` | Get Workitem Tasks |

## backups

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/backups` | Backup Status |

## catalog

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/catalog/artifactory-diagnostics` | Artifactory Diagnostics |
| `GET` | `/api/catalog/cleaners` | List Cleaners |
| `POST` | `/api/catalog/cleaners/{cleaner_id}/edit` | Edit Cleaner |
| `POST` | `/api/catalog/cleaners/{cleaner_id}/remove` | Remove Cleaner |
| `GET` | `/api/catalog/forms` | List Forms |
| `GET` | `/api/catalog/forms/{key}` | Get Form |
| `GET` | `/api/catalog/options/{source}` | Options |
| `GET` | `/api/catalog/quota-preview` | Quota Preview |
| `GET` | `/api/catalog/servicenow-diagnostics` | Servicenow Diagnostics |
| `GET` | `/api/catalog/submissions` | List Submissions |
| `POST` | `/api/catalog/submit/{key}` | Submit |

## dashboards

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/dashboards/` | Get Dashboards |
| `POST` | `/api/dashboards/` | Create Dashboard |
| `GET` | `/api/dashboards/ado-items` | Get Ado Items |
| `GET` | `/api/dashboards/default` | Get Default Dashboard |
| `GET` | `/api/dashboards/snow-items` | Get Snow Items |
| `GET` | `/api/dashboards/widget-types` | Get Widget Types |
| `POST` | `/api/dashboards/widgets/sync` | Sync User Widgets |
| `DELETE` | `/api/dashboards/{dashboard_id}` | Delete Dashboard |
| `PUT` | `/api/dashboards/{dashboard_id}` | Update Dashboard |

## favorites

| Method | Path | What it does |
| --- | --- | --- |
| `DELETE` | `/api/favorites` | Remove Favorite |
| `GET` | `/api/favorites` | List Favorites |
| `POST` | `/api/favorites` | Add Favorite |

## health

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/health` | Basic health check |
| `GET` | `/api/health/` | Basic health check |
| `GET` | `/api/health/live` | Liveness probe |
| `GET` | `/api/health/ready` | Readiness probe |

## inbox

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/inbox` | Inbox |
| `POST` | `/api/inbox/dismiss` | Dismiss |
| `POST` | `/api/inbox/restore` | Restore |

## integrations

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/integrations/artifactory/repo-details` | Artifactory Repo Details |
| `GET` | `/api/integrations/artifactory/repos` | Artifactory Repos |
| `GET` | `/api/integrations/artifactory/storage` | Artifactory Storage |
| `GET` | `/api/integrations/confluence/recent` | Confluence Recent |
| `GET` | `/api/integrations/confluence/search` | Confluence Search |
| `GET` | `/api/integrations/health` | Integrations Health |
| `GET` | `/api/integrations/sonarqube/project-details` | Sonarqube Project Details |
| `GET` | `/api/integrations/sonarqube/projects` | Sonarqube Projects |
| `DELETE` | `/api/integrations/{system}/pins` | Remove Pin |
| `GET` | `/api/integrations/{system}/pins` | List Pins |
| `POST` | `/api/integrations/{system}/pins` | Add Pin |
| `DELETE` | `/api/integrations/{system}/token` | Delete Token |
| `GET` | `/api/integrations/{system}/token` | Get Token Status |
| `POST` | `/api/integrations/{system}/token` | Save Token |

## metrics

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/metrics/approvals` | Get Approval Metrics |
| `GET` | `/api/metrics/services` | Get Service Metrics |
| `GET` | `/api/metrics/usage` | Get Usage Metrics |

## notifications

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/notifications` | List Notifications |
| `POST` | `/api/notifications/read-all` | Mark All Read |
| `GET` | `/api/notifications/unread-count` | Unread Count |
| `POST` | `/api/notifications/{notification_id}/read` | Mark Read |

## observability

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/observability/azure-projects` | List Azure Projects |
| `DELETE` | `/api/observability/data` | Clear Observability Data |
| `GET` | `/api/observability/self-services` | List Self Service Usage |
| `GET` | `/api/observability/summary` | Get Observability Summary |
| `GET` | `/api/observability/tickets` | List Portal Tickets |
| `POST` | `/api/observability/widget` | Record Widget Event |
| `GET` | `/api/observability/widgets` | List Widget Usage |
| `GET` | `/api/observability/widgets/recent` | List Recent Widgets For Me |
| `GET` | `/api/observability/widgets/suggested` | List Suggested Widgets For Me |
| `GET` | `/api/observability/widgets/usage` | List Widget Usage Events |

## pins

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/api/items/mark-seen` | Mark Seen |
| `DELETE` | `/api/pins` | Remove Pin |
| `GET` | `/api/pins` | List Pins |
| `POST` | `/api/pins` | Add Pin |

## quick-links

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/quick-links` | List Quick Links |
| `POST` | `/api/quick-links/mine` | Create My Quick Link |
| `DELETE` | `/api/quick-links/mine/{link_id}` | Delete My Quick Link |
| `PATCH` | `/api/quick-links/mine/{link_id}` | Update My Quick Link |
| `POST` | `/api/quick-links/mine/{link_id}/restore` | Restore My Quick Link |

## safe-mode

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/safe-mode/status` | Get Status |
| `POST` | `/api/safe-mode/toggle` | Toggle Safe Mode |

## search

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/search` | Search |

## suggestions

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/suggestions` | List Suggestions |
| `POST` | `/api/suggestions` | Create Suggestion |
| `DELETE` | `/api/suggestions/{suggestion_id}` | Delete Suggestion |
| `PATCH` | `/api/suggestions/{suggestion_id}` | Update Suggestion |
| `GET` | `/api/suggestions/{suggestion_id}/comments` | List Comments |
| `POST` | `/api/suggestions/{suggestion_id}/comments` | Add Comment |
| `DELETE` | `/api/suggestions/{suggestion_id}/comments/{comment_id}` | Delete Comment |
| `DELETE` | `/api/suggestions/{suggestion_id}/vote` | Remove Vote |
| `POST` | `/api/suggestions/{suggestion_id}/vote` | Add Vote |

## support

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/support/stats` | Get Stats |
| `GET` | `/api/support/status` | Get Status |
| `GET` | `/api/support/tickets` | Get Tickets |
| `POST` | `/api/support/tickets` | Create Ticket |
| `POST` | `/api/support/tickets/create-flow` | Create Ticket Flow |
| `POST` | `/api/support/tickets/reply` | Reply To Ticket |
| `GET` | `/api/support/tickets/{sys_id}` | Get Ticket Detail |
| `GET` | `/api/support/tickets/{sys_id}/attachments/{attachment_id}` | Get Attachment |

## system-urls

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/system-urls` | Get System Urls |

## untagged

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/ui/` | Ui Index |
| `GET` | `/ui/approvals` | Ui Approvals Page |
| `GET` | `/ui/artifactory` | Ui Artifactory Page |
| `GET` | `/ui/audit-logs` | Ui Audit Logs Page |
| `GET` | `/ui/auth` | Ui Auth Page |
| `GET` | `/ui/auth/login` | Ui Auth Login |
| `POST` | `/ui/auth/login` | Ui Auth Local Login |
| `POST` | `/ui/auth/logout` | Ui Auth Logout |
| `GET` | `/ui/automations` | Ui Automations Page |
| `GET` | `/ui/azure-devops` | Ui Azure Devops Page |
| `DELETE` | `/ui/azure-devops/pat` | Ui Delete Ado Pat |
| `GET` | `/ui/azure-devops/pat` | Ui Get Ado Pat Status |
| `POST` | `/ui/azure-devops/pat` | Ui Save Ado Pat |
| `GET` | `/ui/components/artifactory-repos` | Ui Artifactory Repos Component |
| `GET` | `/ui/components/artifactory-storage` | Ui Artifactory Storage Component |
| `GET` | `/ui/components/azure-devops-tasks` | Ui Azure Devops Tasks Component |
| `GET` | `/ui/components/banner` | Ui Banner Component |
| `GET` | `/ui/components/confluence-pages` | Ui Confluence Pages Component |
| `GET` | `/ui/components/pipelines` | Ui Pipelines Component |
| `GET` | `/ui/components/pull-requests` | Ui Pull Requests Component |
| `GET` | `/ui/components/pull-requests-review` | Ui Pull Requests Review Component |
| `GET` | `/ui/components/quick-links` | Ui Quick Links Component |
| `GET` | `/ui/components/recent-activity` | Ui Recent Activity Component |
| `GET` | `/ui/components/servicenow-tickets` | Ui Servicenow Tickets Component |
| `GET` | `/ui/components/sidebar` | Ui Sidebar Component |
| `GET` | `/ui/components/sonarqube-projects` | Ui Sonarqube Projects Component |
| `GET` | `/ui/confluence` | Ui Confluence Page |
| `GET` | `/ui/connections` | Ui Connections Page |
| `GET` | `/ui/dashboard/preferences` | Ui Dashboard Preferences |
| `POST` | `/ui/dashboard/preferences` | Ui Dashboard Preferences Save |
| `GET` | `/ui/grafana` | Ui Grafana Page |
| `GET` | `/ui/internal-aws` | Ui Internal Aws Page |
| `GET` | `/ui/my-requests` | Ui My Requests Page |
| `GET` | `/ui/observability` | Ui Observability Page |
| `GET` | `/ui/openshift` | Ui Openshift Page |
| `GET` | `/ui/platform-managing` | Ui Platform Managing Page |
| `GET` | `/ui/profile` | Ui Profile Page |
| `POST` | `/ui/profile` | Ui Profile Update |
| `GET` | `/ui/search` | Ui Search Page |
| `GET` | `/ui/servicenow` | Ui Servicenow Page |
| `GET` | `/ui/settings` | Ui Settings Page |
| `GET` | `/ui/sonarqube` | Ui Sonarqube Page |
| `GET` | `/ui/suggestions` | Ui Suggestions Page |
| `GET` | `/ui/support` | Ui Support Page |

## user-prefs

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/me` | Get Me |
| `DELETE` | `/api/me/avatar` | Clear Avatar |
| `POST` | `/api/me/avatar` | Set Avatar |
| `PUT` | `/api/me/density` | Set Density |
| `PUT` | `/api/me/display-name` | Set Display Name |
| `PUT` | `/api/me/theme` | Set Theme |

<!-- /GENERATED:API -->
