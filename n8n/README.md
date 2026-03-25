steps to install n8n\


step 1: add n8n helm repo
```
 helm repo add community-charts https://community-charts.github.io/helm-charts
 helm repo update
```

``
helm upgrade --install n8n community-charts/n8n --namespace n8n --create-namespace -f ./n8n/values.yaml           
```

to run the app localy:
```
1. Get the application URL by running these commands:
  export POD_NAME=$(kubectl get pods --namespace n8n -l "app.kubernetes.io/name=n8n,app.kubernetes.io/instance=n8n" -o jsonpath="{.items[0].metadata.name}")
  export CONTAINER_PORT=$(kubectl get pod --namespace n8n $POD_NAME -o jsonpath="{.spec.containers[0].ports[0].containerPort}")
  echo "Visit http://127.0.0.1:8080 to use your application"
  kubectl --namespace n8n port-forward $POD_NAME 8080:$CONTAINER_PORT

```