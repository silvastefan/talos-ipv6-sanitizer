talos-ipv6-sanitizer/
├── Dockerfile
├── main.py
├── requirements.txt
└── k8s
    ├── 01-serviceaccount.yaml
    ├── 02-clusterrole.yaml
    ├── 03-clusterrolebinding.yaml
    └── 04-deployment.yaml
    
Passo a Passo de Deploy
1. Clone ou baixe seu repositório localmente:
   1. git clone https://github.com/silvastefan/talos-ipv6-sanitizer.git
   2. cd talos-ipv6-sanitizer
2. Monte a imagem Docker 
   docker build -t stefansilva/talos-ipv6-sanitizer .
3. Faça o push da imagem para o seu registry (Docker Hub, GitHub Container Registry, etc.):
   docker push stefansilva/talos-ipv6-sanitizer:latest
4. Monte a imagem Docker para arm / x86
   docker buildx build \
  --platform=linux/amd64 \
  -t stefansilva/talos-ipv6-sanitizer:latest \
  . \
  --push
5. Edite o arquivo 04-deployment.yaml na parte de image: "seu-registro/talos-ipv6-sanitizer:latest" para apontar para a imagem que você acabou de enviar.
6. Instale (apply) os manifests no Kubernetes:
   1. kubectl apply -f k8s/01-serviceaccount.yaml
   2. kubectl apply -f k8s/02-clusterrole.yaml
   3. kubectl apply -f k8s/03-clusterrolebinding.yaml
   4. kubectl apply -f k8s/04-deployment.yaml
7. Verifique se o Deployment foi criado corretamente:
   1. kubectl get pods -l app=talos-ipv6-sanitizer
8. Confira os logs para confirmar que o código está rodando:
   1. kubectl logs -f deployment/talos-ipv6-sanitizer-deployment
9. Sempre que um Node novo (ou modificado) for detectado com a anotação
alpha.kubernetes.io/provided-node-ip contendo IPv6, o controller irá removê-lo e manter somente o(s) IPv4.
10. criar node worker e verificar se o ipv6 vai ser removido de forma automatica
hcloud server create --name talos-worker-template-v15 \
--image ${IMAGE_ID} \
--type cx22 \
--location hel1 \
--label 'type=worker' \
--network 10824191 \
--user-data-from-file worker.yaml