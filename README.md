# 🛡️ Talos IPv6 Sanitizer Operator

Este operador foi desenvolvido para clusters Kubernetes utilizando **Talos Linux** + **Hetzner Cloud Controller Manager**, onde o `providerID` não é gerado automaticamente quando a anotação `alpha.kubernetes.io/provided-node-ip` inclui o IPv6.

Este operador detecta automaticamente os nodes recém-criados que contenham múltiplos IPs na anotação, e realiza a limpeza mantendo apenas o IPv4 — forçando assim o Hetzner CCM a gerar corretamente o `providerID`.

---

## ✅ Benefícios

- Garante geração automática do `providerID` em nodes Talos.
- Remove conflitos causados por múltiplos IPs no campo `alpha.kubernetes.io/provided-node-ip`.
- Automatiza um processo crítico para ambientes com escalonamento automático.
- Código simples, direto, e com estrutura pronta para expansão futura.

---

## 🔧 Pré-requisitos

- Kubernetes 1.21+ com acesso de administrador (`kubectl`)
- Cluster com Talos Linux (v1.9+)
- Python 3.9+ (para desenvolvimento/testes locais)
- Helm 3.x
- [Opcional] ArgoCD, FluxCD ou GitOps para gerenciar o operador

---

## 🚀 Instalação com Helm

```bash
# Adicione o repositório
helm repo add talos-operator https://seu-endereco.com/talos-operator

# Instale o operador
helm install talos-ipv6-sanitizer talos-operator/ \
  --namespace kube-system \
  --create-namespace

# Upgrade
helm upgrade talos-ipv6-sanitizer talos-operator/ \
  --namespace kube-system
```

---

## 🧪 Teste Manual

```bash
kubectl annotate node NOME-DO-NODE \
  alpha.kubernetes.io/provided-node-ip="10.0.0.8,2a01:4f9:c010:ac47::1" --overwrite
```

---

## 📌 Observações Técnicas

- O operador utiliza a API do Kubernetes para monitorar eventos Node em tempo real.
- Sempre que um node está com status Ready e a anotação provided-node-ip contiver `,`, ele é corrigido.
- Caso já esteja correto, nada é feito.
- O código está pronto para futuras validações adicionais (como adicionar/remover labels, aplicar taints, etc).

---

## 📈 Comparativo: Por que usar Helm Chart?

| Critério       | Benefícios com Helm Chart |
|----------------|----------------------------|
| **Escalabilidade** | Fácil adicionar novos recursos e serviços |
| **Reutilização** | Pode ser usado em múltiplos clusters/ambientes com valores diferentes |
| **Organização** | Diretórios bem definidos (`templates/`, `values.yaml`, etc.) |
| **Parâmetros** | Permite customizar comportamentos via `values.yaml` |
| **Atualizações** | Fácil de versionar, publicar e atualizar com `helm upgrade` |
| **Integração** | Compatível com GitOps, ArgoCD, Flux, etc. |

---

## 💡 Exemplo de evolução futura

Você pode adicionar lógica para:
- Configurar taints
- Ajustar labels automaticamente
- Adicionar nodeselectors
- Criar alertas quando providerID não for gerado
- Criar dashboards no Grafana com base nesse operador

---

## 📁 Estrutura do Projeto

```
talos-ipv6-sanitizer/
├── charts/
│   └── talos-ipv6-sanitizer/
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           └── deployment.yaml
├── src/
│   └── main.py
│   └── requirements.txt
└── README.md
```

---

## 🤝 Contribuições

Este projeto é 100% open source e aberto para melhorias. Envie um PR ou abra uma issue para discutir novas ideias.

---

## 🧠 Inspirado em

- Comunidade Talos Linux
- Projetos reais enfrentando problemas com IPv6 no Hetzner CCM
- Usuários que prezam por uma infraestrutura limpa, resiliente e automatizada.

---

**Feito com ☕ por IVA AI** – Você pensa, nós criamos.
