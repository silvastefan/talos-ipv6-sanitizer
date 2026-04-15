"""
DAG: s3_monitor_e_processar
============================
Monitora um prefixo (pasta) em um bucket S3 e, ao detectar novos arquivos,
executa um processamento. Após o processamento:

  - SUCESSO → arquivos movidos para o prefixo 'processados/'
  - FALHA   → arquivos movidos para o prefixo 'erros/' + callback de notificação

Configuração
------------
Defina as variáveis abaixo no Airflow (Admin → Variables) para sobrescrever
os valores padrão. Alternativamente, passe os parâmetros diretamente ao
disparar a DAG manualmente (Trigger DAG w/ config).

  Variável Airflow          Padrão               Descrição
  ─────────────────────     ──────────────────   ──────────────────────────────
  s3_bucket                 meu-bucket           Nome do bucket S3
  s3_monitor_prefix         incoming/            Prefixo/pasta a monitorar
  s3_processed_prefix       processed/           Destino dos arquivos com sucesso
  s3_error_prefix           errors/              Destino dos arquivos com falha
  s3_file_pattern           *                    Filtro por nome (ex: *.csv)
  aws_conn_id               aws_default          ID da conexão AWS no Airflow

Fluxo de execução
-----------------
  monitorar_s3  →  processar_arquivos  →  mover_para_processados
                          │
                          └──(falha)──→  mover_para_erros
                                              │
                                              └──→  notificar_falha
"""

import fnmatch
import logging
from datetime import timedelta
from typing import List

from airflow import DAG
from airflow.models import Variable
from airflow.operators.python import PythonOperator
from airflow.sensors.base import BaseSensorOperator
from airflow.utils.dates import days_ago

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Valores padrão (sobrescrevíveis via Airflow Variables)
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_BUCKET = Variable.get("s3_bucket", default_var="meu-bucket")
_DEFAULT_PREFIX = Variable.get("s3_monitor_prefix", default_var="incoming/")
_DEFAULT_PROCESSED = Variable.get("s3_processed_prefix", default_var="processed/")
_DEFAULT_ERRORS = Variable.get("s3_error_prefix", default_var="errors/")
_DEFAULT_PATTERN = Variable.get("s3_file_pattern", default_var="*")
_DEFAULT_CONN = Variable.get("aws_conn_id", default_var="aws_default")


# ─────────────────────────────────────────────────────────────────────────────
# Sensor customizado — verifica novos arquivos no S3
# ─────────────────────────────────────────────────────────────────────────────
class S3NovosArquivosSensor(BaseSensorOperator):
    """
    Sensor que verifica periodicamente se há arquivos em um prefixo S3.

    Quando arquivos são encontrados, armazena as chaves no XCom
    ('arquivos_encontrados') e retorna True para liberar as tarefas seguintes.

    Parâmetros
    ----------
    bucket_name : str
        Nome do bucket S3.
    prefix : str
        Prefixo (pasta) a ser monitorado. Ex: 'incoming/' ou 'dados/entrada/'.
    file_pattern : str
        Filtro por nome de arquivo (glob). Ex: '*.csv', 'relatorio_*.xlsx' ou '*'.
    aws_conn_id : str
        ID da conexão AWS configurada no Airflow.
    """

    template_fields = ("bucket_name", "prefix", "file_pattern")

    def __init__(
        self,
        bucket_name: str,
        prefix: str,
        file_pattern: str = "*",
        aws_conn_id: str = "aws_default",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.bucket_name = bucket_name
        self.prefix = prefix
        self.file_pattern = file_pattern
        self.aws_conn_id = aws_conn_id

    def poke(self, context) -> bool:
        from airflow.providers.amazon.aws.hooks.s3 import S3Hook

        hook = S3Hook(aws_conn_id=self.aws_conn_id)

        self.log.info(
            "Verificando s3://%s/%s | padrão: '%s'",
            self.bucket_name,
            self.prefix,
            self.file_pattern,
        )

        chaves = hook.list_keys(bucket_name=self.bucket_name, prefix=self.prefix) or []

        # Ignora entradas que representam apenas subpastas (terminam com '/')
        arquivos = [k for k in chaves if not k.endswith("/")]

        # Aplica filtro de nome quando definido
        if self.file_pattern and self.file_pattern != "*":
            arquivos = [
                f for f in arquivos
                if fnmatch.fnmatch(f.split("/")[-1], self.file_pattern)
            ]

        if not arquivos:
            self.log.info("Nenhum arquivo encontrado. Aguardando próxima verificação...")
            return False

        self.log.info("Encontrado(s) %d arquivo(s): %s", len(arquivos), arquivos)
        context["ti"].xcom_push(key="arquivos_encontrados", value=arquivos)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Tarefas Python
# ─────────────────────────────────────────────────────────────────────────────
def _get_s3_conn(params: dict):
    """Retorna (hook S3, nome do bucket) a partir dos params da DAG."""
    from airflow.providers.amazon.aws.hooks.s3 import S3Hook

    bucket = params.get("bucket", _DEFAULT_BUCKET)
    aws_conn_id = params.get("aws_conn_id", _DEFAULT_CONN)
    return S3Hook(aws_conn_id=aws_conn_id), bucket


def _mover_arquivo_s3(hook, bucket: str, origem: str, destino: str) -> None:
    """
    Move um objeto S3: copia para o destino e remove o original.
    (S3 não possui operação nativa de 'mover'.)
    """
    s3 = hook.get_conn()
    s3.copy_object(
        CopySource={"Bucket": bucket, "Key": origem},
        Bucket=bucket,
        Key=destino,
    )
    s3.delete_object(Bucket=bucket, Key=origem)
    log.info("Movido: s3://%s/%s  →  s3://%s/%s", bucket, origem, bucket, destino)


def processar_arquivos(**context) -> None:
    """
    Processa cada arquivo encontrado pelo sensor.

    Os arquivos são obtidos via XCom da tarefa 'monitorar_s3'.
    Após o processamento, a lista de arquivos processados é publicada
    no XCom ('arquivos_processados') para uso nas tarefas seguintes.

    ╔══════════════════════════════════════════════════════════════════╗
    ║  CUSTOMIZE AQUI: substitua o bloco marcado com sua lógica.      ║
    ╚══════════════════════════════════════════════════════════════════╝
    """
    ti = context["ti"]
    params = context["params"]
    hook, bucket = _get_s3_conn(params)

    arquivos: List[str] = ti.xcom_pull(
        task_ids="monitorar_s3", key="arquivos_encontrados"
    )

    if not arquivos:
        raise ValueError("Sensor não retornou arquivos para processar.")

    processados = []

    for chave in arquivos:
        log.info("── Iniciando processamento: s3://%s/%s", bucket, chave)

        # ──────────────────────────────────────────────────────────────────
        # INÍCIO DA LÓGICA DE NEGÓCIO
        # Leia o arquivo e aplique sua transformação/validação/carga.
        #
        # Exemplos:
        #   CSV com pandas:
        #     import pandas as pd, io
        #     corpo = hook.get_key(chave, bucket_name=bucket).get()["Body"].read()
        #     df = pd.read_csv(io.BytesIO(corpo))
        #     # ... processa df ...
        #
        #   Invocar API externa:
        #     import requests
        #     r = requests.post("https://api.exemplo.com/processar", json={"arquivo": chave})
        #     r.raise_for_status()
        #
        #   Invocar Lambda AWS:
        #     import boto3
        #     lam = boto3.client("lambda")
        #     lam.invoke(FunctionName="minha-funcao", Payload=json.dumps({"chave": chave}))
        # ──────────────────────────────────────────────────────────────────
        obj = hook.get_key(chave, bucket_name=bucket)
        conteudo = obj.get()["Body"].read()
        log.info(
            "Arquivo lido com sucesso (%d bytes). Aplicando lógica de negócio...",
            len(conteudo),
        )
        # FIM DA LÓGICA DE NEGÓCIO ─────────────────────────────────────

        processados.append(chave)
        log.info("── Processamento concluído: %s", chave)

    ti.xcom_push(key="arquivos_processados", value=processados)
    log.info("Total processado: %d arquivo(s).", len(processados))


def mover_para_processados(**context) -> None:
    """
    Move os arquivos para o prefixo de 'processados' após sucesso.
    Executada somente quando 'processar_arquivos' conclui sem erros.
    """
    ti = context["ti"]
    params = context["params"]
    hook, bucket = _get_s3_conn(params)
    destino_base = params.get("prefixo_processados", _DEFAULT_PROCESSED).rstrip("/")

    arquivos: List[str] = ti.xcom_pull(
        task_ids="processar_arquivos", key="arquivos_processados"
    ) or []

    if not arquivos:
        log.warning("Nenhum arquivo para mover para 'processados'.")
        return

    for chave in arquivos:
        nome = chave.split("/")[-1]
        destino = f"{destino_base}/{nome}"
        _mover_arquivo_s3(hook, bucket, chave, destino)

    log.info("%d arquivo(s) movido(s) para '%s/'.", len(arquivos), destino_base)


def mover_para_erros(**context) -> None:
    """
    Move os arquivos para o prefixo de 'erros' quando 'processar_arquivos' falha.
    Executada somente quando a tarefa de processamento levanta uma exceção.
    """
    ti = context["ti"]
    params = context["params"]
    hook, bucket = _get_s3_conn(params)
    destino_base = params.get("prefixo_erros", _DEFAULT_ERRORS).rstrip("/")

    # Tenta obter a lista de arquivos encontrados pelo sensor
    arquivos: List[str] = (
        ti.xcom_pull(task_ids="monitorar_s3", key="arquivos_encontrados") or []
    )

    if not arquivos:
        log.warning("Nenhum arquivo para mover para 'erros'.")
        return

    for chave in arquivos:
        nome = chave.split("/")[-1]
        destino = f"{destino_base}/{nome}"
        try:
            _mover_arquivo_s3(hook, bucket, chave, destino)
        except Exception as exc:
            log.error(
                "Falha ao mover '%s' para erros: %s. "
                "O arquivo permanece em s3://%s/%s.",
                chave, exc, bucket, chave,
            )

    log.info("%d arquivo(s) movido(s) para '%s/'.", len(arquivos), destino_base)


def notificar_falha(**context) -> None:
    """
    Registra detalhes da falha e aciona canal de notificação.

    Executada somente quando 'processar_arquivos' falha (trigger_rule=one_failed).

    ╔══════════════════════════════════════════════════════════════════╗
    ║  CUSTOMIZE AQUI: adicione o canal de notificação desejado.      ║
    ╚══════════════════════════════════════════════════════════════════╝
    """
    dag_run = context.get("dag_run")
    ti = context.get("ti")

    log.error(
        "═══════════════════════════════════════════════════\n"
        "  FALHA NO PROCESSAMENTO S3\n"
        "  DAG     : %s\n"
        "  Run ID  : %s\n"
        "  Tarefa  : %s\n"
        "═══════════════════════════════════════════════════",
        dag_run.dag_id if dag_run else "N/A",
        dag_run.run_id if dag_run else "N/A",
        ti.task_id if ti else "N/A",
    )

    # ──────────────────────────────────────────────────────────────────
    # INÍCIO DAS NOTIFICAÇÕES — adicione o(s) canal(is) desejado(s)
    #
    # ► Slack (requer provider: apache-airflow-providers-slack)
    #   from airflow.providers.slack.hooks.slack_webhook import SlackWebhookHook
    #   slack = SlackWebhookHook(slack_webhook_conn_id="slack_default")
    #   slack.send(text=f":x: *Falha no processamento S3*\n"
    #                   f"DAG: `{dag_run.dag_id}` | Run: `{dag_run.run_id}`")
    #
    # ► E-mail (configure smtp_* no airflow.cfg ou via variáveis de ambiente)
    #   from airflow.utils.email import send_email
    #   send_email(
    #       to=["equipe@empresa.com"],
    #       subject=f"[Airflow] Falha: {dag_run.dag_id}",
    #       html_content=f"<p>Run ID: {dag_run.run_id}</p>",
    #   )
    #
    # ► SNS (requer provider: apache-airflow-providers-amazon)
    #   from airflow.providers.amazon.aws.hooks.sns import SnsHook
    #   sns = SnsHook(aws_conn_id="aws_default")
    #   sns.publish_to_target(
    #       target_arn="arn:aws:sns:us-east-1:123:meu-topico",
    #       message=f"Falha na DAG {dag_run.dag_id}",
    #   )
    # ──────────────────────────────────────────────────────────────────
    log.error("Nenhum canal de notificação configurado. Adicione em notificar_falha().")


# ─────────────────────────────────────────────────────────────────────────────
# Callback de falha global (acionado em QUALQUER tarefa que falhe)
# ─────────────────────────────────────────────────────────────────────────────
def _on_failure_callback(context):
    dag_run = context.get("dag_run")
    task_instance = context.get("task_instance")
    exception = context.get("exception")

    log.error(
        "Falha detectada | DAG: %s | Run: %s | Tarefa: %s | Erro: %s",
        dag_run.dag_id if dag_run else "N/A",
        dag_run.run_id if dag_run else "N/A",
        task_instance.task_id if task_instance else "N/A",
        exception,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Definição da DAG
# ─────────────────────────────────────────────────────────────────────────────
with DAG(
    dag_id="s3_monitor_e_processar",
    description="Monitora prefixo S3, processa arquivos e move conforme resultado",
    schedule_interval=timedelta(minutes=5),  # Frequência de verificação
    start_date=days_ago(1),
    catchup=False,
    max_active_runs=1,  # Apenas uma execução simultânea para evitar duplicatas
    default_args={
        "owner": "airflow",
        "depends_on_past": False,
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
        "on_failure_callback": _on_failure_callback,
    },
    # Parâmetros configuráveis por execução (Trigger DAG w/ config)
    params={
        "bucket": _DEFAULT_BUCKET,
        "prefixo_monitorado": _DEFAULT_PREFIX,
        "prefixo_processados": _DEFAULT_PROCESSED,
        "prefixo_erros": _DEFAULT_ERRORS,
        "padrao_arquivo": _DEFAULT_PATTERN,
        "aws_conn_id": _DEFAULT_CONN,
    },
    tags=["s3", "monitoramento", "etl"],
) as dag:

    # ── 1. Sensor: aguarda novos arquivos no S3 ───────────────────────────
    monitorar_s3 = S3NovosArquivosSensor(
        task_id="monitorar_s3",
        bucket_name="{{ params.bucket }}",
        prefix="{{ params.prefixo_monitorado }}",
        file_pattern="{{ params.padrao_arquivo }}",
        aws_conn_id="{{ params.aws_conn_id }}",
        poke_interval=30,       # Verifica a cada 30 segundos
        timeout=60 * 60 * 4,   # Timeout após 4 horas sem arquivo
        mode="reschedule",      # Libera o worker entre verificações (não bloqueia slot)
        soft_fail=True,         # Timeout → tarefa SKIPPED, não FAILED
        doc_md=(
            "Aguarda novos arquivos no prefixo S3 configurado. "
            "Quando encontrados, publica as chaves no XCom e libera o fluxo."
        ),
    )

    # ── 2. Processa os arquivos encontrados ──────────────────────────────
    processar = PythonOperator(
        task_id="processar_arquivos",
        python_callable=processar_arquivos,
        provide_context=True,
        doc_md="Executa a lógica de negócio sobre cada arquivo encontrado.",
    )

    # ── 3a. SUCESSO: move para 'processados/' ────────────────────────────
    mover_processados = PythonOperator(
        task_id="mover_para_processados",
        python_callable=mover_para_processados,
        provide_context=True,
        # Executada somente quando 'processar_arquivos' conclui com sucesso (padrão)
        doc_md="Move os arquivos para o prefixo 'processados/' após sucesso.",
    )

    # ── 3b. FALHA: move para 'erros/' ────────────────────────────────────
    mover_erros = PythonOperator(
        task_id="mover_para_erros",
        python_callable=mover_para_erros,
        provide_context=True,
        trigger_rule="one_failed",  # Executada somente se 'processar_arquivos' falhar
        doc_md="Move os arquivos para o prefixo 'erros/' quando o processamento falha.",
    )

    # ── 4. FALHA: notificação ─────────────────────────────────────────────
    notificar = PythonOperator(
        task_id="notificar_falha",
        python_callable=notificar_falha,
        provide_context=True,
        trigger_rule="one_failed",  # Executada somente se alguma tarefa anterior falhar
        doc_md="Envia notificação de falha no processamento.",
    )

    # ─────────────────────────────────────────────────────────────────────
    # Fluxo:
    #   monitorar_s3 → processar → mover_para_processados   (caminho feliz)
    #                        └──→ mover_para_erros           (em caso de falha)
    #                        └──→ notificar_falha            (em caso de falha)
    # ─────────────────────────────────────────────────────────────────────
    monitorar_s3 >> processar >> mover_processados
    processar >> [mover_erros, notificar]
