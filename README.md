# Airflow — Monitoramento de Bucket S3

DAG do Airflow que monitora um prefixo (pasta) em um bucket S3 e, ao detectar
novos arquivos, executa uma lógica de processamento configurável.

## Fluxo de execução

```
┌─────────────────┐
│  Sensor S3      │  Verifica a cada 30s se há arquivos no prefixo monitorado
│  (monitorar_s3) │  Timeout configurável (padrão: 4 horas)
└────────┬────────┘
         │ arquivos encontrados
         ▼
┌─────────────────────┐
│  Processar Arquivos │  Executa a lógica de negócio (personalizável)
└────────┬────────────┘
         │
    ┌────┴──────────────────────────────┐
    │ SUCESSO                           │ FALHA
    ▼                                   ▼
┌──────────────────┐         ┌──────────────────┐   ┌─────────────────┐
│ Mover para       │         │ Mover para       │   │ Notificar       │
│ processados/     │         │ erros/           │   │ Falha           │
└──────────────────┘         └──────────────────┘   └─────────────────┘
```

**Garantias:**
- Arquivo com falha **nunca** é movido para `processados/`
- `max_active_runs=1` impede processamento duplicado simultâneo
- Sensor em modo `reschedule` (não bloqueia slots de worker)

## Início rápido

### Pré-requisitos
- Docker e Docker Compose
- Acesso ao bucket S3 com permissões `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`

### 1. Configurar variáveis de ambiente

```bash
cp .env.example .env
# Edite .env com suas credenciais AWS e configurações do bucket
```

### 2. Subir o ambiente

```bash
# Inicializar banco de dados e criar usuário admin
docker compose up airflow-init

# Subir scheduler e webserver
docker compose up -d
```

Acesse **http://localhost:8080** com `admin / admin`.

### 3. Configurar conexão AWS

No Airflow: **Admin → Connections → +**

| Campo       | Valor                          |
|-------------|--------------------------------|
| Conn ID     | `aws_default`                  |
| Conn Type   | `Amazon Web Services`          |
| Login       | `AWS_ACCESS_KEY_ID`            |
| Password    | `AWS_SECRET_ACCESS_KEY`        |
| Extra       | `{"region_name": "us-east-1"}` |

> **Alternativa:** defina `AIRFLOW_CONN_AWS_DEFAULT` no `.env` (veja o `docker-compose.yaml`).

### 4. Configurar variáveis da DAG

No Airflow: **Admin → Variables** — adicione conforme necessário:

| Variável                | Exemplo            | Descrição                         |
|-------------------------|--------------------|-----------------------------------|
| `s3_bucket`             | `meu-bucket`       | Nome do bucket                    |
| `s3_monitor_prefix`     | `incoming/`        | Pasta a monitorar                 |
| `s3_processed_prefix`   | `processed/`       | Destino dos arquivos com sucesso  |
| `s3_error_prefix`       | `errors/`          | Destino dos arquivos com falha    |
| `s3_file_pattern`       | `*.csv`            | Filtro de nome (glob)             |
| `aws_conn_id`           | `aws_default`      | ID da conexão AWS                 |

> As variáveis de ambiente `AIRFLOW_VAR_*` no `docker-compose.yaml` já definem
> os valores padrão automaticamente a partir do `.env`.

### 5. Ativar a DAG

Na interface do Airflow, ative a toggle da DAG **`s3_monitor_e_processar`**.

---

## Personalizar a lógica de processamento

Edite a função `processar_arquivos()` em [`dags/s3_monitor_dag.py`](dags/s3_monitor_dag.py)
no bloco marcado:

```python
# ──────────────────────────────────────────────────────────────────
# INÍCIO DA LÓGICA DE NEGÓCIO
# ──────────────────────────────────────────────────────────────────
obj = hook.get_key(chave, bucket_name=bucket)
conteudo = obj.get()["Body"].read()
# ... sua lógica aqui ...
# ──────────────────────────────────────────────────────────────────
```

Exemplos de integrações disponíveis nos comentários do arquivo.

---

## Configurar notificações de falha

Edite a função `notificar_falha()` em [`dags/s3_monitor_dag.py`](dags/s3_monitor_dag.py).
Descomente o bloco correspondente ao canal desejado (Slack, e-mail, SNS).

Para Slack, adicione o provider no [`requirements-airflow.txt`](requirements-airflow.txt):
```
apache-airflow-providers-slack==8.7.1
```

---

## Disparar manualmente com parâmetros diferentes

Na interface: **DAGs → s3_monitor_e_processar → Trigger DAG w/ config**

```json
{
  "bucket": "outro-bucket",
  "prefixo_monitorado": "dados/entrada/",
  "prefixo_processados": "dados/saida/ok/",
  "prefixo_erros": "dados/saida/erro/",
  "padrao_arquivo": "relatorio_*.csv",
  "aws_conn_id": "aws_producao"
}
```

---

## Estrutura do projeto

```
.
├── dags/
│   └── s3_monitor_dag.py        # DAG principal (sensor + processamento + mover)
├── logs/                        # Logs do Airflow (gerado automaticamente)
├── plugins/                     # Plugins customizados (opcional)
├── docker-compose.yaml          # Ambiente local (Airflow + PostgreSQL)
├── requirements-airflow.txt     # Dependências extras instaladas na imagem
├── .env.example                 # Template de variáveis de ambiente
└── .env                        # Suas credenciais (não versionar!)
```

---

## Permissões IAM necessárias

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::meu-bucket"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:CopyObject"
      ],
      "Resource": "arn:aws:s3:::meu-bucket/*"
    }
  ]
}
```
