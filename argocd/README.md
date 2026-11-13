# Argo CD – TicketMEI

O Argo CD acompanha a pasta `k8s` da branch `main` deste repositório e compara
os manifestos publicados com os recursos do namespace `ticketmei`.
A instalação usa a versão **v3.5.7**, fixada no Kustomize, num namespace próprio
(`argocd`). O projeto `ticketmei` permite apenas este repositório e este destino.

## 1. Instalar no Docker Desktop

Ativa o Kubernetes no Docker Desktop. Na raiz do projeto:

```sh
# Criar o namespace e instalar o Argo CD
kubectl --context docker-desktop apply -f argocd/install/namespace.yaml
kubectl --context docker-desktop apply --server-side --field-manager=argocd-bootstrap -k argocd/install

# Aguardar os componentes
kubectl --context docker-desktop -n argocd wait --for=condition=Established crd/applications.argoproj.io crd/appprojects.argoproj.io --timeout=60s
kubectl --context docker-desktop -n argocd rollout status deployment/argocd-redis --timeout=300s
kubectl --context docker-desktop -n argocd rollout status deployment/argocd-repo-server --timeout=300s
kubectl --context docker-desktop -n argocd rollout status deployment/argocd-server --timeout=300s
kubectl --context docker-desktop -n argocd rollout status statefulset/argocd-application-controller --timeout=300s

# Depois de o pipeline publicar as imagens compatíveis, registar a aplicação
kubectl --context docker-desktop apply -k argocd/apps
kubectl --context docker-desktop -n argocd get applications
```

Os ficheiros de instalação ficam fora de `k8s`, para que a aplicação TicketMEI
gira apenas os seus serviços. O Redis interno do Argo CD (`argocd-redis`) é
independente do Redis da aplicação (`redis`, no namespace `ticketmei`).

## 2. Abrir a interface

```sh
kubectl --context docker-desktop -n argocd port-forward svc/argocd-server 8443:443
```

Mantém este terminal aberto e acede a **https://localhost:8443**.
O certificado TLS inicial é autoassinado. O utilizador é **`admin`**.
Para obter a password inicial, noutro terminal:

```sh
kubectl --context docker-desktop -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 --decode
```
para windows
```sh
 $p = kubectl --context docker-desktop -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}'
>> [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p))
```

A password é gerada pelo Argo CD e fica no Secret do cluster.

## 3. Publicar e sincronizar alterações

A aplicação está configurada para **sincronização automática**, com `selfHeal`
ativo e `prune` desativado. Alterar os ficheiros locais não altera o estado
pretendido pelo Argo CD: é preciso publicar as alterações na `main`.
O estado **OutOfSync** indica diferenças entre o Git e o cluster; **Healthy**
indica que os recursos observados estão saudáveis. Estes estados são independentes.

1. Revê e publica as alterações na `main`, incluindo o workflow e os manifestos do Redis.
2. Aguarda que o GitHub Actions termine a construção das imagens e atualize as tags em `k8s`.
3. Confirma que as novas imagens suportam a arquitetura do cluster.
4. Aplica `kubectl --context docker-desktop apply -k argocd/apps` para registar esta configuração do Argo CD, apenas no primeiro arranque ou quando a própria Application mudar.
5. O Argo CD aplica automaticamente os manifestos de `k8s`. Na interface, confirma **Synced** e **Healthy**.

Para demonstrar o fluxo completo, altera um serviço e publica a alteração na
`main`. Confirma o GitHub Actions, o commit automático com a nova tag e o rollout
no Argo CD, sem carregar em **Sync** nem aplicar os workloads com `kubectl`.

Os Deployments de base usam imagens `ticketmei-*:local`, mas a secção `images`
de `k8s/kustomization.yaml` substitui as imagens dos serviços já publicados
por imagens do GHCR. Estas substituições prevalecem sobre as imagens locais.
Para construir imagens locais, podes usar:

```sh
docker build -t ticketmei-api-gateway:local ./api-gateway
docker build -t ticketmei-catalog:local ./catalog
docker build -t ticketmei-order:local ./order
docker build -t ticketmei-notification:local ./notification
```

O Argo CD aplica manifestos; a construção e publicação de imagens é um passo
separado. Se usares imagens do GHCR, confirma que o cluster consegue descarregá-las.
O Job k6 continua fora da aplicação e só é executado quando pedido.

O workflow `.github/workflows/cd.yaml` publica imagens para `linux/amd64` e
`linux/arm64`, identificadas pelo Git SHA. Uma alteração ao próprio workflow
reconstrói os quatro serviços, para substituir também imagens antigas que só
suportavam uma arquitetura. Em **GitHub → Actions → ci → Run workflow**, podes
também iniciar a reconstrução completa na branch `main`.

Se aparecer `ImagePullBackOff` com `no match for platform in manifest`, confirma
que o pipeline terminou e que os manifestos já apontam para as novas tags:

```sh
docker buildx imagetools inspect ghcr.io/joaocatanho/ticketmei-api-gateway:<GIT_SHA>
```

O resultado deve incluir `linux/arm64` para o Docker Desktop neste Mac.
Numa instalação antiga ainda em modo manual, aplica a configuração automática
apenas depois de publicar os manifestos e confirmar as imagens compatíveis.

A política definida em `argocd/apps/ticketmei.yaml` é:

```yaml
automated:
  enabled: true
  prune: false
  selfHeal: true
```

Com esta opção, novos commits são aplicados automaticamente e alterações manuais
ao cluster são corrigidas a partir do Git. A remoção automática de recursos
continua desativada.

## 4. Diagnóstico

```sh
kubectl --context docker-desktop -n argocd get pods
kubectl --context docker-desktop -n argocd get application ticketmei -o yaml
kubectl --context docker-desktop -n argocd logs deployment/argocd-repo-server --tail=100
kubectl --context docker-desktop -n argocd logs statefulset/argocd-application-controller --tail=100
```

Se o repositório passar a privado, configura as credenciais em **Settings →
Repositories** no Argo CD. O Secret com essas credenciais deve ficar no cluster.

Referências: [instalação oficial](https://argo-cd.readthedocs.io/en/stable/getting_started/),
[versão v3.5.7](https://github.com/argoproj/argo-cd/releases/tag/v3.5.7) e
[sincronização automática](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/).
