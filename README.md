# S3 Monitor — Airflow DAG

Solução de monitoramento e processamento automático de arquivos em bucket AWS S3,
implementada como DAG do Apache Airflow.

Ao detectar novos arquivos — na raiz de uma pasta ou dentro de subpastas —,
a DAG executa uma lógica de processamento configurável e move cada arquivo para
o destino correto conforme o resultado: pasta de processados (com partição por data)
em caso de sucesso, ou pasta de erros em caso de falha.

---

## Sumário

1. [Visão Geral](#visão-geral)
2. [Fluxo de Execução](#fluxo-de-execução)
3. [Estrutura de Pastas no S3](#estrutura-de-pastas-no-s3)
4. [Início Rápido](#início-rápido)
5. [Configuração AWS](#configuração-aws)
6. [Parâmetros e Variáveis](#parâmetros-e-variáveis)
7. [Modos de Processamento](#modos-de-processamento)
8. [Customização](#customização)
9. [Estrutura do Projeto](#estrutura-do-projeto)
10. [Permissões IAM](#permissões-iam)
11. [Troubleshooting](#troubleshooting)

---

## Visão Geral

### Problema resolvido

Em pipelines de dados é comum receber arquivos em um bucket S3 — vindos de parceiros,
sistemas legados, dispositivos IoT, etc. — e precisar processá-los automaticamente
assim que chegam. Os desafios típicos são:

- Detectar novos arquivos de forma confiável (raiz e subpastas)
- Processar cada arquivo e garantir rastreabilidade do resultado
- Não mover arquivos que falharam para a pasta de "processados"
- Notificar a equipe quando algo dá errado
- Suportar processamento sequencial (quando a ordem importa) e paralelo (quando a velocidade importa)

### Solução

Uma DAG do Airflow com três tarefas encadeadas:

| Tarefa | Tipo | Função |
|--------|------|--------|
| `monitorar_s3` | Sensor customizado | Detecta novos arquivos no S3 (recursivo, com filtro por nome) |
| `processar_arquivos` | PythonOperator | Executa a lógica de negócio e move cada arquivo independentemente |
| `notificar_falha` | PythonOperator | Notifica a equipe em caso de falha (Slack, e-mail ou SNS) |

### Garantias do sistema

| Garantia | Como é implementada |
|----------|---------------------|
| Arquivo com falha nunca vai para `processed/` | Cada arquivo é movido individualmente dentro da função de processamento |
| Falha de um arquivo não bloqueia os demais | Cada arquivo tem seu próprio `try/except`; o loop (ou pool de threads) continua |
| Sem processamento duplicado simultâneo | `max_active_runs=1` na DAG |
| Rastreabilidade por data | Destino dos processados inclui `AAAA/MM/DD/` baseado no `logical_date` da DAG run |
| Subpastas preservadas | Caminhos calculados preservando a estrutura relativa ao prefixo monitorado |

---

## Fluxo de Execução

```mermaid
flowchart TD
    SCHEDULER(["`**Scheduler**
    Dispara a cada 5 minutos`"])

    SCHEDULER --> SENSOR

    subgraph TASK1 ["📡  Tarefa 1 — monitorar_s3"]
        SENSOR{"`Verifica o prefixo S3
        a cada **30 segundos**`"}
        AGUARDA["`Worker liberado
        *(modo reschedule)*`"]
        SENSOR -- "Sem arquivos" --> AGUARDA
        AGUARDA --> SENSOR
    end

    SENSOR -- "⏰ Timeout 4h" --> SKIPPED(["`**SKIPPED**
    *(sem arquivos no período)*`"])

    SENSOR -- "✅ Arquivos encontrados" --> TASK2

    subgraph TASK2 ["⚙️  Tarefa 2 — processar_arquivos"]
        MODO{"`**max_paralelo?**`"}
        SEQ["`Sequencial
        *(um por vez)*`"]
        PAR["`Paralelo
        *(N simultâneos)*`"]
        MODO -- "= 0" --> SEQ
        MODO -- "> 0" --> PAR
        SEQ --> PROC
        PAR --> PROC
        PROC["`Para cada arquivo:
        executa a **lógica de negócio**`"]
        PROC -- "✅" --> MOV_OK["`Move para
        **processed/AAAA/MM/DD/**`"]
        PROC -- "❌" --> MOV_ERR["`Move para
        **errors/**`"]
        MOV_OK --> FIM_PROC
        MOV_ERR --> FIM_PROC
        FIM_PROC{"`Houve
        alguma falha?`"}
    end

    FIM_PROC -- "Não" --> SUCCESS(["`DAG: ✅ **SUCCESS**`"])
    FIM_PROC -- "Sim" --> TASK3

    subgraph TASK3 ["🔔  Tarefa 3 — notificar_falha  *(só se falhar)*"]
        NOTIF["`Lê detalhes das falhas do XCom
        e envia **notificação**
        Slack / E-mail / SNS`"]
    end

    TASK3 --> FAILED(["`DAG: ❌ **FAILED**`"])
```

### Cenários de execução

| Cenário | Resultado da DAG |
|---------|-----------------|
| Nenhum arquivo em 4h | `SUCCESS` — `monitorar_s3` marcada como `SKIPPED` |
| Todos os arquivos processados com sucesso | `SUCCESS` — arquivos em `processed/AAAA/MM/DD/` |
| Alguns arquivos falham (falha parcial) | `FAILED` — arquivos ok em `processed/`, falhos em `errors/`, notificação enviada |
| Todos os arquivos falham | `FAILED` — todos em `errors/`, notificação enviada |

> Para mais detalhes técnicos e diagramas adicionais, consulte [docs/arquitetura.md](docs/arquitetura.md).

---

## Estrutura de Pastas no S3

```
bucket-s3/
│
├── incoming/                          ← Pasta monitorada
│   ├── relatorio.csv                  ← Arquivo na raiz
│   └── financeiro/                    ← Subpasta (suportada)
│       └── jan_2024.parquet
│
├── processed/                         ← Destino: SUCESSO
│   └── 2024/
│       └── 01/
│           └── 15/                    ← Partição por data (logical_date)
│               ├── relatorio.csv
│               └── financeiro/        ← Estrutura preservada
│                   └── jan_2024.parquet
│
└── errors/                            ← Destino: FALHA
    ├── relatorio.csv                  ← Sem partição por data
    └── financeiro/
        └── jan_2024.parquet
```

**Por que a data de execução (não `datetime.now()`)?**
O destino usa o `logical_date` da DAG run — a data de agendamento — e não
o horário atual. Isso garante que reprocessamentos e backfills sempre usem
a data correta do período processado.

---

## Início Rápido

### Pré-requisitos

- Docker e Docker Compose instalados
- Acesso ao bucket S3 com as [permissões IAM necessárias](#permissões-iam)
- Python 3.9+ (apenas para desenvolvimento local sem Docker)

### 1. Clonar e configurar variáveis de ambiente

```bash
git clone <url-do-repositorio>
cd <nome-do-repositorio>

# Copia o template e edita com suas credenciais
cp .env.example .env
```

Edite o arquivo `.env` com as configurações do seu ambiente:

```ini
# Credenciais AWS
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
AWS_DEFAULT_REGION=us-east-1

# Configuração do bucket
S3_BUCKET=meu-bucket-producao
S3_MONITOR_PREFIX=incoming/
S3_PROCESSED_PREFIX=processed/
S3_ERROR_PREFIX=errors/
S3_FILE_PATTERN=*
```

### 2. Inicializar e subir o ambiente

```bash
# Inicializa o banco de metadados e cria o usuário admin
docker compose up airflow-init

# Sobe o scheduler e o webserver em background
docker compose up -d
```

Acesse a interface em **http://localhost:8080**

| Campo | Valor |
|-------|-------|
| Usuário | `admin` |
| Senha | `admin` |

### 3. Configurar a conexão AWS

No Airflow: **Admin → Connections → Adicionar (+)**

| Campo | Valor |
|-------|-------|
| Connection ID | `aws_default` |
| Connection Type | `Amazon Web Services` |
| Login | Seu `AWS_ACCESS_KEY_ID` |
| Password | Seu `AWS_SECRET_ACCESS_KEY` |
| Extra | `{"region_name": "us-east-1"}` |

> **Alternativa via variável de ambiente:** descomente a linha
> `AIRFLOW_CONN_AWS_DEFAULT` no `docker-compose.yaml`.

### 4. Ativar a DAG

Na interface do Airflow, localize a DAG **`s3_monitor_e_processar`** e ative
a toggle no lado esquerdo.

A DAG começará a monitorar o bucket a cada 5 minutos.

---

## Configuração AWS

### Opção A — Credenciais estáticas (desenvolvimento)

Configure no `.env` e a conexão `aws_default` no Airflow como descrito acima.

### Opção B — IAM Role (produção recomendada)

Em produção, é preferível não usar credenciais estáticas. Atribua uma IAM Role
à instância EC2, ECS task ou pod Kubernetes onde o Airflow roda.

O `S3Hook` do Airflow usa o boto3, que resolve credenciais automaticamente na
seguinte ordem:
1. Variáveis de ambiente (`AWS_ACCESS_KEY_ID`, etc.)
2. Perfil `~/.aws/credentials`
3. IAM Role da instância/task/pod
4. IAM Role via IMDS (EC2 Instance Metadata)

Com IAM Role, crie a conexão `aws_default` sem Login/Password (deixe em branco)
e o boto3 usará as credenciais da Role automaticamente.

### Opção C — Múltiplos ambientes (dev/homolog/prod)

Crie uma conexão para cada ambiente no Airflow:

| Connection ID | Ambiente |
|---------------|----------|
| `aws_dev` | Desenvolvimento |
| `aws_homolog` | Homologação |
| `aws_producao` | Produção |

Selecione a conexão desejada ao disparar a DAG via `aws_conn_id` nos parâmetros.

---

## Parâmetros e Variáveis

### Airflow Variables (configuração global)

Defina em **Admin → Variables**. São os valores padrão usados em todas as execuções.

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `s3_bucket` | `meu-bucket` | Nome do bucket S3 |
| `s3_monitor_prefix` | `incoming/` | Pasta a monitorar (deve terminar com `/`) |
| `s3_processed_prefix` | `processed/` | Destino dos arquivos com sucesso |
| `s3_error_prefix` | `errors/` | Destino dos arquivos com falha |
| `s3_file_pattern` | `*` | Filtro de nome (glob). Ex: `*.csv`, `dados_*.json` |
| `aws_conn_id` | `aws_default` | ID da conexão AWS no Airflow |

> As variáveis `AIRFLOW_VAR_*` no `docker-compose.yaml` já populam essas variáveis
> automaticamente a partir do arquivo `.env`.

### Parâmetros por execução (sobrescrevem as Variables)

Ao disparar a DAG manualmente (**Trigger DAG w/ config**), passe um JSON
para sobrescrever as configurações para aquela execução específica:

```json
{
  "bucket":              "bucket-producao",
  "prefixo_monitorado":  "dados/entrada/",
  "prefixo_processados": "dados/saida/ok/",
  "prefixo_erros":       "dados/saida/erro/",
  "padrao_arquivo":      "relatorio_*.csv",
  "aws_conn_id":         "aws_producao",
  "max_paralelo":        4
}
```

---

## Modos de Processamento

O parâmetro `max_paralelo` controla como os arquivos são processados:

### Sequencial (`max_paralelo = 0` — padrão)

```
arquivo_1  →  processa  →  move
arquivo_2  →  processa  →  move     (só começa após arquivo_1 terminar)
arquivo_3  →  processa  →  move     (só começa após arquivo_2 terminar)
```

**Use quando:**
- A ordem de processamento importa
- Os arquivos compartilham um recurso (ex: mesmo banco sem lock por linha)
- A lógica de negócio não é thread-safe
- Há poucos arquivos e velocidade não é crítica

### Paralelo (`max_paralelo = N`)

```
arquivo_1  →  processa  →  move  ┐
arquivo_2  →  processa  →  move  ├─ executam simultaneamente
arquivo_3  →  processa  →  move  ┘
```

**Use quando:**
- Os arquivos são independentes entre si
- O processamento envolve I/O (leitura S3, chamada de API, consulta banco)
- Velocidade de processamento é prioritária
- A lógica de negócio é thread-safe

**Recomendações de valor:**
- `2` ou `4`: bom ponto de partida para APIs externas
- `8` a `16`: adequado para operações puramente S3-to-S3
- Evite valores muito altos se houver limite de conexões no destino (banco, API)

> O modo paralelo usa `ThreadPoolExecutor` — ideal para operações I/O bound.
> Para workloads CPU bound intensivos, considere uma abordagem com múltiplos workers Celery.

---

## Customização

### Lógica de negócio

Edite a função `_processar_um_arquivo()` no arquivo
[`dags/s3_monitor_dag.py`](dags/s3_monitor_dag.py).

Localize o bloco demarcado e substitua pelo seu código:

```python
# ══════════════════════════════════════════════════════════════════
# INÍCIO DA LÓGICA DE NEGÓCIO
# ══════════════════════════════════════════════════════════════════

obj = hook.get_key(chave, bucket_name=bucket)
conteudo = obj.get()["Body"].read()

# Substitua a partir daqui com sua lógica...
# Qualquer exceção não tratada será capturada e o arquivo irá para errors/

# FIM DA LÓGICA DE NEGÓCIO ════════════════════════════════════════
```

**Exemplos disponíveis nos comentários do código:**
- Leitura de CSV com pandas e processamento do DataFrame
- Chamada a API externa com validação do status HTTP
- Invocação de Lambda AWS com payload customizado
- Carga em banco de dados PostgreSQL via hook do Airflow

### Canal de notificação

Edite a função `notificar_falha()` no mesmo arquivo. Descomente o bloco
do canal desejado:

```python
# ══════════════════════════════════════════════════════════════════
# INÍCIO DOS CANAIS DE NOTIFICAÇÃO
# ══════════════════════════════════════════════════════════════════

# ── Opção A: Slack ────────────────────────────────────────────────
# SlackWebhookHook(slack_webhook_conn_id="slack_default").send(text=mensagem)

# ── Opção B: E-mail ───────────────────────────────────────────────
# send_email(to=["equipe@empresa.com"], subject="...", html_content="...")

# ── Opção C: AWS SNS ──────────────────────────────────────────────
# SnsHook(aws_conn_id="aws_default").publish_to_target(target_arn="...", message="...")

# ══════════════════════════════════════════════════════════════════
```

Para Slack e SNS, adicione os providers necessários em
[`requirements-airflow.txt`](requirements-airflow.txt).

---

## Estrutura do Projeto

```
.
├── dags/
│   └── s3_monitor_dag.py        # DAG principal — toda a lógica da solução
│
├── docs/
│   └── arquitetura.md           # Documentação técnica com diagramas detalhados
│
├── logs/                        # Logs do Airflow (gerado automaticamente)
├── plugins/                     # Plugins customizados do Airflow (opcional)
│
├── docker-compose.yaml          # Ambiente local: Airflow 2.9 + PostgreSQL
├── requirements-airflow.txt     # Dependências Python instaladas na imagem
│
├── .env.example                 # Template de variáveis de ambiente
├── .env                         # Suas credenciais (NÃO versionar — está no .gitignore)
└── .gitignore                   # Protege .env e artefatos de build
```

### Arquivo principal: `dags/s3_monitor_dag.py`

```
s3_monitor_dag.py
│
├── [Docstring de módulo]         Visão geral completa da solução
│
├── Constantes _DEFAULT_*         Valores padrão das Airflow Variables
│
├── class S3NovosArquivosSensor   Sensor customizado (Tarefa 1)
│   └── poke()                    Lógica de verificação do S3
│
├── _calcular_destino_processados() Calcula caminho com partição de data
├── _calcular_destino_erros()       Calcula caminho para pasta de erros
├── _mover_arquivo_s3()             Copia + deleta (simula "mover" no S3)
│
├── _processar_um_arquivo()       Núcleo: processa 1 arquivo (customizável)
│   └── [LÓGICA DE NEGÓCIO]       ← CUSTOMIZE AQUI
│
├── processar_arquivos()          Orquestrador (Tarefa 2): sequencial ou paralelo
│
├── notificar_falha()             Notificação de falha (Tarefa 3)
│   └── [CANAIS DE NOTIFICAÇÃO]   ← CUSTOMIZE AQUI
│
├── _on_failure_callback()        Callback global de falha
│
└── with DAG(...):                Definição da DAG e das tarefas
    ├── monitorar_s3              Instância do sensor
    ├── processar                 PythonOperator → processar_arquivos()
    ├── notificar                 PythonOperator → notificar_falha()
    └── monitorar_s3 >> processar >> notificar   (definição do fluxo)
```

---

## Permissões IAM

A IAM Role ou usuário IAM usado pelo Airflow precisa das seguintes permissões:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListarBucket",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::nome-do-bucket"
    },
    {
      "Sid": "OperacoesObjetos",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:CopyObject"
      ],
      "Resource": "arn:aws:s3:::nome-do-bucket/*"
    }
  ]
}
```

| Permissão | Por que é necessária |
|-----------|---------------------|
| `s3:ListBucket` | Sensor lista objetos no prefixo monitorado |
| `s3:GetObject` | Lê o conteúdo do arquivo para processamento |
| `s3:PutObject` | Cria a cópia no destino (processed/ ou errors/) |
| `s3:DeleteObject` | Remove o arquivo original após copiar (simula "mover") |
| `s3:CopyObject` | Implícito via `PutObject` no destino com CopySource |

> Para princípio do menor privilégio, restrinja o `Resource` aos prefixos
> específicos em vez de usar `/*`:
> - `arn:aws:s3:::bucket/incoming/*` para GetObject e DeleteObject
> - `arn:aws:s3:::bucket/processed/*` para PutObject
> - `arn:aws:s3:::bucket/errors/*` para PutObject

---

## Troubleshooting

### DAG não aparece na interface

- Verifique se não há erro de sintaxe Python: `python dags/s3_monitor_dag.py`
- Confirme que o arquivo está na pasta `dags/` mapeada no Docker Compose
- Aguarde até 30 segundos para o scheduler atualizar a lista de DAGs

### Sensor fica em `running` indefinidamente

- Verifique a conexão AWS: Admin → Connections → Test
- Confirme que o bucket e prefixo existem e estão acessíveis
- Verifique as permissões IAM (`s3:ListBucket`)
- Confira os logs da tarefa: clique na tarefa → Log

### Arquivo não é detectado pelo sensor

- Verifique se o arquivo está no prefixo exato configurado em `prefixo_monitorado`
- Confirme o padrão glob em `padrao_arquivo` — ex: `*.CSV` não detecta `arquivo.csv` (case-sensitive no S3)
- Arquivos que terminam com `/` são ignorados (marcadores de pasta vazia)

### Arquivo processado mas não movido

- Verifique as permissões `s3:PutObject` e `s3:DeleteObject`
- Confirme nos logs da tarefa se houve erro na operação de mover
- Verifique se os prefixos `processed/` e `errors/` existem no bucket

### Processamento lento com muitos arquivos

- Aumente `max_paralelo` nos parâmetros da DAG (ex: `4` ou `8`)
- Verifique limites de concorrência na API/banco de destino
- Considere aumentar os recursos do worker Airflow

### DAG dispara mas não processa (XCom vazio)

- Verifique se a tarefa `monitorar_s3` concluiu com `success` (não `skipped`)
- Confirme que o sensor encontrou e publicou os arquivos no XCom
- Nos logs do sensor, procure a linha `"Encontrado(s) N arquivo(s)"`
