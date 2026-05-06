-- Initialize service health tracking

INSERT INTO service_health (service_name, status) VALUES
('azure_devops', 'UNKNOWN'),
('sonarqube', 'UNKNOWN'),
('artifactory', 'UNKNOWN'),
('servicenow', 'UNKNOWN');

