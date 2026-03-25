Here are all the steps i did in order to bring up Sonarqube:

step 1: configure postgresql
```
# 1. First, do a dry-run to see if the YAML renders correctly for Autopilot                                                                
>> helm install sonar-infra ./sonarqube --namespace sonarqube --create-namespace --dry-run --debug
>>
>> # 2. If it looks good, deploy it
>> helm upgrade --install sonar-infra ./sonarqube --namespace sonarqube
```
step 2: install sonarqube
```
helm repo add sonarqube https://SonarSource.github.io/helm-chart-sonarqube                                                                 
>> helm repo update
```

step 3: deploy sonarqube
```
 helm upgrade --install sonarqube sonarqube/sonarqube \                                                                                     
>>     --namespace sonarqube \
>>     -f .\sonarqube-deployment\values.yaml
```


extra step, in order to delete helm installation:
```
helm uninstall sonar-infra -n sonarqube                             
```
