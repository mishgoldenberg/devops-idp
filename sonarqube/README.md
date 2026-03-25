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
to access the app localy:
```
1. Get the application URL by running these commands:
  export POD_NAME=$(kubectl get pods --namespace sonarqube -l "app=sonarqube,release=sonarqube" -o jsonpath="{.items[0].metadata.name}")
  echo "Visit http://127.0.0.1:8080 to use your application"
  kubectl port-forward $POD_NAME 8080:9000 -n sonarqube
```