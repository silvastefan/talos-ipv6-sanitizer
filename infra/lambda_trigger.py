"""
Lambda: s3_airflow_trigger
==========================

Função AWS Lambda que recebe notificações do S3 via Amazon EventBridge (ou S3
Event Notifications direto) e dispara a DAG do Airflow via REST API.

Fluxo completo (event-driven):
-------------------------------

    ┌─────────────────┐    S3 Event    ┌─────────────────┐   REST API   ┌──────────────┐
    │  Arquivo chega  │ ─────────────▶ │  Lambda         │ ───────────▶ │  Airflow     │
    │  no S3          │                │  (este arquivo) │              │  DAG run     │
    └─────────────────┘                └─────────────────┘              └──────────────┘

Vantagens sobre o modelo de polling:
--------------------------------------
  • Sem execuções desnecessárias: a DAG só roda quando há arquivo real
  • Sem falsos alertas: sem timeouts, sem tarefas SKIPPED/FAILED por ausência de arquivo
  • Latência mínima: arquivo detectado em segundos (não minutos/horas)
  • Independente da frequência: funciona para arquivos mensais, diários ou imprevisíveis

Configuração necessária (variáveis de ambiente do Lambda):
----------------------------------------------------------
  AIRFLOW_BASE_URL      URL base do Airflow. Ex: https://airflow.empresa.com
  AIRFLOW_DAG_ID        ID da DAG a disparar. Ex: s3_monitor_e_processar
  AIRFLOW_USERNAME      Usuário com permissão de trigger na API do Airflow
  AIRFLOW_PASSWORD      Senha do usuário acima
  AIRFLOW_CONN_ID       (opcional) ID da conexão AWS a usar na DAG. Padrão: aws_default

  Alternativa segura para credenciais: armazene AIRFLOW_USERNAME e AIRFLOW_PASSWORD
  no AWS Secrets Manager e leia via boto3 (ver função _get_credenciais_airflow abaixo).

Configuração do gatilho S3 → Lambda:
--------------------------------------
  Opção A (recomendada): Amazon EventBridge
    1. S3 → habilitar "Amazon EventBridge" nas propriedades do bucket
    2. EventBridge → criar regra com padrão:
       {
         "source": ["aws.s3"],
         "detail-type": ["Object Created"],
         "detail": {
           "bucket": {"name": ["nome-do-bucket"]},
           "object": {"key": [{"prefix": "incoming/"}]}
         }
       }
    3. Target da regra: esta função Lambda

  Opção B: S3 Event Notifications diretamente
    1. Bucket → Properties → Event notifications → Create event notification
    2. Prefix: incoming/
    3. Events: s3:ObjectCreated:*
    4. Destination: Lambda function → esta função

Permissões IAM da função Lambda (execution role):
--------------------------------------------------
  {
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:ListBucket"],
    "Resource": ["arn:aws:s3:::nome-do-bucket", "arn:aws:s3:::nome-do-bucket/incoming/*"]
  }

  Se usar Secrets Manager para credenciais:
  {
    "Effect": "Allow",
    "Action": ["secretsmanager:GetSecretValue"],
    "Resource": "arn:aws:secretsmanager:REGIAO:CONTA:secret:airflow-credentials-*"
  }
"""

import json
import logging
import os
import urllib.request
import urllib.error
from base64 import b64encode
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

log = logging.getLogger()
log.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────────────────────────
# Configuração — lida de variáveis de ambiente do Lambda
# ─────────────────────────────────────────────────────────────────────────────
AIRFLOW_BASE_URL = os.environ.get("AIRFLOW_BASE_URL", "http://localhost:8080")
AIRFLOW_DAG_ID   = os.environ.get("AIRFLOW_DAG_ID",   "s3_monitor_e_processar")
AIRFLOW_USERNAME = os.environ.get("AIRFLOW_USERNAME",  "admin")
AIRFLOW_PASSWORD = os.environ.get("AIRFLOW_PASSWORD",  "admin")
AIRFLOW_CONN_ID  = os.environ.get("AIRFLOW_CONN_ID",   "aws_default")


def _get_credenciais_airflow() -> tuple[str, str]:
    """
    Retorna (username, password) para autenticação na API do Airflow.

    Por padrão lê das variáveis de ambiente. Para produção, substitua por
    uma leitura do AWS Secrets Manager (código comentado abaixo).

    Retorna
    -------
    tuple[str, str]
        (username, password) para Basic Auth na REST API do Airflow.
    """
    # ── Opção recomendada para produção: AWS Secrets Manager ──────────────
    # import boto3, json
    # cliente = boto3.client("secretsmanager")
    # segredo = cliente.get_secret_value(SecretId="airflow-api-credentials")
    # dados = json.loads(segredo["SecretString"])
    # return dados["username"], dados["password"]
    # ─────────────────────────────────────────────────────────────────────

    return AIRFLOW_USERNAME, AIRFLOW_PASSWORD


def _extrair_info_arquivo(evento: Dict) -> Optional[Dict]:
    """
    Extrai informações do arquivo a partir do evento recebido pelo Lambda.

    Suporta dois formatos de evento:
      - EventBridge (recomendado): detail-type = "Object Created"
      - S3 Event Notification direta: Records[0].s3

    Parâmetros
    ----------
    evento : dict
        Evento recebido pelo Lambda handler.

    Retorna
    -------
    dict ou None
        {"bucket": str, "chave": str} se o evento for válido.
        None se o formato não for reconhecido.
    """
    # ── Formato EventBridge ────────────────────────────────────────────────
    # {"source": "aws.s3", "detail-type": "Object Created",
    #  "detail": {"bucket": {"name": "..."}, "object": {"key": "..."}}}
    if evento.get("source") == "aws.s3" and "detail" in evento:
        detail = evento["detail"]
        bucket = detail.get("bucket", {}).get("name")
        chave  = detail.get("object", {}).get("key")
        if bucket and chave:
            return {"bucket": bucket, "chave": chave}

    # ── Formato S3 Event Notification direta ──────────────────────────────
    # {"Records": [{"s3": {"bucket": {"name": "..."}, "object": {"key": "..."}}}]}
    registros = evento.get("Records", [])
    if registros and "s3" in registros[0]:
        s3 = registros[0]["s3"]
        bucket = s3.get("bucket", {}).get("name")
        # A chave vem URL-encoded no S3 Event Notification
        chave  = s3.get("object", {}).get("key", "").replace("+", " ")
        chave  = urllib.parse.unquote(chave) if chave else None
        if bucket and chave:
            return {"bucket": bucket, "chave": chave}

    return None


def _disparar_dag(bucket: str, chave: str) -> Dict:
    """
    Dispara uma execução da DAG no Airflow via REST API (POST /dags/{dag_id}/dagRuns).

    Passa o bucket e a chave do arquivo como configuração da DAG run, permitindo
    que o sensor saiba exatamente qual arquivo processar.

    Parâmetros
    ----------
    bucket : str
        Nome do bucket S3 onde o arquivo chegou.
    chave : str
        Chave S3 completa do arquivo. Ex: 'incoming/relatorio.csv'

    Retorna
    -------
    dict
        Resposta da API do Airflow com os detalhes da DAG run criada.

    Levanta
    -------
    urllib.error.HTTPError
        Se a API retornar um erro HTTP (ex: 401 não autorizado, 409 já existe).
    RuntimeError
        Se a DAG run não puder ser criada por outro motivo.
    """
    username, password = _get_credenciais_airflow()

    # Monta o Basic Auth header
    credencial = b64encode(f"{username}:{password}".encode()).decode()
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Basic {credencial}",
    }

    # ID único da DAG run: evita duplicatas se o Lambda for invocado duas vezes
    # para o mesmo arquivo (comportamento "at least once" do S3/EventBridge)
    chave_segura = chave.replace("/", "_").replace(".", "_")
    run_id = f"s3_trigger__{chave_segura}__{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    # Corpo da requisição: passa os parâmetros que a DAG usará
    corpo = {
        "dag_run_id": run_id,
        "conf": {
            # O sensor usará esses valores para confirmar o arquivo
            "bucket":             bucket,
            "prefixo_monitorado": "/".join(chave.split("/")[:-1]) + "/",
            "padrao_arquivo":     chave.split("/")[-1],  # nome exato do arquivo
            "aws_conn_id":        AIRFLOW_CONN_ID,
        },
        "note": f"Disparado automaticamente via Lambda — arquivo: s3://{bucket}/{chave}",
    }

    url = f"{AIRFLOW_BASE_URL.rstrip('/')}/api/v1/dags/{quote(AIRFLOW_DAG_ID)}/dagRuns"

    log.info("Disparando DAG '%s' | Run ID: %s | URL: %s", AIRFLOW_DAG_ID, run_id, url)
    log.info("Configuração da DAG run: %s", json.dumps(corpo["conf"], indent=2))

    requisicao = urllib.request.Request(
        url,
        data=json.dumps(corpo).encode(),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(requisicao, timeout=30) as resposta:
            corpo_resposta = json.loads(resposta.read().decode())
            log.info(
                "DAG run criada com sucesso | Run ID: %s | Estado: %s",
                corpo_resposta.get("dag_run_id"),
                corpo_resposta.get("state"),
            )
            return corpo_resposta

    except urllib.error.HTTPError as e:
        corpo_erro = e.read().decode()
        log.error("Erro HTTP %d ao disparar a DAG: %s", e.code, corpo_erro)
        raise


def lambda_handler(evento: Dict, contexto: Any) -> Dict:
    """
    Ponto de entrada da função Lambda.

    Recebe o evento do S3 (via EventBridge ou S3 Event Notification),
    extrai as informações do arquivo e dispara a DAG do Airflow.

    Parâmetros
    ----------
    evento : dict
        Evento enviado pelo S3/EventBridge ao Lambda. Dois formatos suportados:
          - EventBridge Object Created
          - S3 Event Notification (Records[])
    contexto : LambdaContext
        Contexto de execução do Lambda (não utilizado diretamente).

    Retorna
    -------
    dict
        {"statusCode": 200, "body": "..."} em caso de sucesso.
        {"statusCode": 400/500, "body": "..."} em caso de erro.
    """
    log.info("Evento recebido: %s", json.dumps(evento, default=str))

    # Extrai as informações do arquivo do evento
    info = _extrair_info_arquivo(evento)

    if not info:
        mensagem = "Formato de evento não reconhecido. Esperado: EventBridge ou S3 Event Notification."
        log.error(mensagem)
        return {"statusCode": 400, "body": mensagem}

    bucket = info["bucket"]
    chave  = info["chave"]

    log.info("Arquivo detectado: s3://%s/%s", bucket, chave)

    try:
        resultado = _disparar_dag(bucket=bucket, chave=chave)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "mensagem": "DAG disparada com sucesso",
                "dag_run_id": resultado.get("dag_run_id"),
                "arquivo": f"s3://{bucket}/{chave}",
            }),
        }

    except Exception as exc:
        log.error("Falha ao disparar a DAG: %s", exc)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "mensagem": "Falha ao disparar a DAG",
                "erro": str(exc),
                "arquivo": f"s3://{bucket}/{chave}",
            }),
        }
