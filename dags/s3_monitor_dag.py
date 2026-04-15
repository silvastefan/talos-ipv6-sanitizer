"""
DAG: s3_monitor_e_processar
============================
Monitora um prefixo (pasta) em um bucket S3 e, ao detectar novos arquivos
(inclusive dentro de subpastas), processa cada um de forma independente.

Resultado por arquivo:
  - SUCESSO → movido para 'processados/' preservando estrutura de subpastas
  - FALHA   → movido para 'erros/'     preservando estrutura de subpastas

Fluxo de execução:
  monitorar_s3  →  processar_arquivos  →  notificar_falha (apenas se houver falha)

Modo de processamento (parâmetro 'max_paralelo'):
  0  → sequencial: um arquivo de cada vez (padrão seguro)
  N  → paralelo:   até N arquivos simultaneamente

Exemplos de entrada suportados:
  incoming/arquivo.csv                   ← arquivo na raiz do prefixo
  incoming/relatorio/dados.csv           ← arquivo dentro de subpasta
  incoming/2024/01/arquivo.parquet       ← arquivo em subpasta aninhada

Configuração via Airflow Variables (Admin → Variables):
  Variável                Padrão          Descrição
  ─────────────────────   ───────────     ──────────────────────────────────────
  s3_bucket               meu-bucket      Nome do bucket S3
  s3_monitor_prefix       incoming/       Prefixo/pasta a monitorar
  s3_processed_prefix     processed/      Destino dos arquivos com sucesso
  s3_error_prefix         errors/         Destino dos arquivos com falha
  s3_file_pattern         *               Filtro glob (ex: *.csv, dados_*.json)
  aws_conn_id             aws_default     ID da conexão AWS no Airflow
"""

import fnmatch
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Dict, List

from airflow import DAG
from airflow.exceptions import AirflowException
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
# Sensor — verifica novos arquivos no S3 (recursivo, inclui subpastas)
# ─────────────────────────────────────────────────────────────────────────────
class S3NovosArquivosSensor(BaseSensorOperator):
    """
    Sensor que verifica periodicamente se há arquivos em um prefixo S3.

    A listagem é recursiva: detecta arquivos tanto na raiz do prefixo quanto
    dentro de subpastas em qualquer nível de profundidade.

    Quando encontra arquivos, armazena as chaves completas no XCom
    ('arquivos_encontrados') e retorna True para liberar as tarefas seguintes.

    Parâmetros
    ----------
    bucket_name : str
        Nome do bucket S3.
    prefix : str
        Prefixo (pasta) a ser monitorado. Ex: 'incoming/' ou 'dados/entrada/'.
    file_pattern : str
        Filtro por nome de arquivo (glob). Aplicado apenas ao nome do arquivo,
        não ao caminho completo. Ex: '*.csv', 'relatorio_*.xlsx' ou '*'.
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

        # list_keys é recursivo: retorna todos os objetos sob o prefixo,
        # incluindo os que estão dentro de subpastas.
        chaves = hook.list_keys(bucket_name=self.bucket_name, prefix=self.prefix) or []

        # Ignora marcadores de "pasta vazia" (chaves que terminam com '/')
        arquivos = [k for k in chaves if not k.endswith("/")]

        # Aplica filtro de nome (somente sobre o nome do arquivo, não o caminho)
        if self.file_pattern and self.file_pattern != "*":
            arquivos = [
                f for f in arquivos
                if fnmatch.fnmatch(f.split("/")[-1], self.file_pattern)
            ]

        if not arquivos:
            self.log.info("Nenhum arquivo encontrado. Aguardando próxima verificação...")
            return False

        self.log.info("Encontrado(s) %d arquivo(s):", len(arquivos))
        for a in arquivos:
            self.log.info("  s3://%s/%s", self.bucket_name, a)

        context["ti"].xcom_push(key="arquivos_encontrados", value=arquivos)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Utilitários S3
# ─────────────────────────────────────────────────────────────────────────────
def _calcular_destino_processados(
    chave: str, prefixo_origem: str, prefixo_processados: str, data_execucao: str
) -> str:
    """
    Calcula o caminho de destino para arquivos processados com sucesso.

    Inclui partição por data (ano/mes/dia) com base na data de execução da DAG,
    preservando a estrutura de subpastas relativa ao prefixo monitorado.

    Parâmetros
    ----------
    data_execucao : str
        Data no formato 'YYYY/MM/DD', extraída do logical_date da DAG run.

    Exemplos (data_execucao = '2024/01/15'):
        "incoming/arquivo.csv"         → "processed/2024/01/15/arquivo.csv"
        "incoming/pasta/arquivo.csv"   → "processed/2024/01/15/pasta/arquivo.csv"
        "incoming/a/b/arquivo.parquet" → "processed/2024/01/15/a/b/arquivo.parquet"
    """
    caminho_relativo = chave[len(prefixo_origem):]
    return f"{prefixo_processados.rstrip('/')}/{data_execucao}/{caminho_relativo}"


def _calcular_destino_erros(chave: str, prefixo_origem: str, prefixo_erros: str) -> str:
    """
    Calcula o caminho de destino para arquivos que falharam.

    Preserva a estrutura de subpastas, sem partição por data.

    Exemplos:
        "incoming/arquivo.csv"       → "errors/arquivo.csv"
        "incoming/pasta/arquivo.csv" → "errors/pasta/arquivo.csv"
    """
    caminho_relativo = chave[len(prefixo_origem):]
    return f"{prefixo_erros.rstrip('/')}/{caminho_relativo}"


def _mover_arquivo_s3(hook, bucket: str, origem: str, destino: str) -> None:
    """
    Move um objeto S3: copia para o destino e remove o original.
    (O S3 não possui operação nativa de 'mover'.)
    """
    s3 = hook.get_conn()
    s3.copy_object(
        CopySource={"Bucket": bucket, "Key": origem},
        Bucket=bucket,
        Key=destino,
    )
    s3.delete_object(Bucket=bucket, Key=origem)
    log.info("Movido: s3://%s/%s  →  s3://%s/%s", bucket, origem, bucket, destino)


# ─────────────────────────────────────────────────────────────────────────────
# Processamento individual de cada arquivo
# ─────────────────────────────────────────────────────────────────────────────
def _processar_um_arquivo(
    chave: str,
    bucket: str,
    aws_conn_id: str,
    prefixo_origem: str,
    prefixo_processados: str,
    prefixo_erros: str,
    data_execucao: str,
) -> Dict:
    """
    Processa um único arquivo e o move para o destino adequado.

    Retorna um dicionário com o resultado:
        {"chave": str, "status": "sucesso"|"falha", "destino": str, "erro"?: str}

    Esta função é chamada tanto no modo sequencial quanto no modo paralelo,
    criando um S3Hook próprio para ser thread-safe.

    ╔══════════════════════════════════════════════════════════════════╗
    ║  CUSTOMIZE AQUI: substitua o bloco de lógica de negócio.        ║
    ╚══════════════════════════════════════════════════════════════════╝
    """
    from airflow.providers.amazon.aws.hooks.s3 import S3Hook

    # Cada chamada cria seu próprio hook (thread-safe no modo paralelo)
    hook = S3Hook(aws_conn_id=aws_conn_id)

    log.info("── Iniciando: s3://%s/%s", bucket, chave)

    try:
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
        #     r = requests.post("https://api.exemplo.com/processar", json={"chave": chave})
        #     r.raise_for_status()
        #
        #   Invocar Lambda AWS:
        #     import boto3, json
        #     lam = boto3.client("lambda")
        #     lam.invoke(FunctionName="minha-funcao", Payload=json.dumps({"chave": chave}))
        # ──────────────────────────────────────────────────────────────────
        obj = hook.get_key(chave, bucket_name=bucket)
        conteudo = obj.get()["Body"].read()
        log.info("Arquivo lido (%d bytes). Aplicando lógica de negócio...", len(conteudo))
        # FIM DA LÓGICA DE NEGÓCIO ─────────────────────────────────────

        # Sucesso: move para 'processados/ano/mes/dia/' preservando subpastas
        destino = _calcular_destino_processados(
            chave, prefixo_origem, prefixo_processados, data_execucao
        )
        _mover_arquivo_s3(hook, bucket, chave, destino)
        log.info("── Sucesso: %s", chave)
        return {"chave": chave, "status": "sucesso", "destino": destino}

    except Exception as exc:
        log.error("── Falha: %s | Erro: %s", chave, exc)

        # Falha: move para 'erros/' preservando subpastas (sem partição por data)
        destino_erro = _calcular_destino_erros(chave, prefixo_origem, prefixo_erros)
        try:
            _mover_arquivo_s3(hook, bucket, chave, destino_erro)
        except Exception as move_exc:
            # Se não conseguir mover para erros, o arquivo permanece no lugar original
            log.error(
                "Não foi possível mover '%s' para erros: %s. "
                "Arquivo permanece em s3://%s/%s.",
                chave, move_exc, bucket, chave,
            )
            destino_erro = chave  # informa que ficou no lugar original

        return {
            "chave": chave,
            "status": "falha",
            "destino": destino_erro,
            "erro": str(exc),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Tarefas da DAG
# ─────────────────────────────────────────────────────────────────────────────
def processar_arquivos(**context) -> None:
    """
    Processa todos os arquivos encontrados pelo sensor.

    Modo de execução controlado pelo parâmetro 'max_paralelo':
      0  → sequencial: um arquivo por vez (ordem garantida, sem concorrência)
      N  → paralelo:   até N arquivos processados simultaneamente

    Cada arquivo é tratado de forma independente:
      - Arquivos que concluem com sucesso são movidos para 'processados/'
      - Arquivos que falham são movidos para 'erros/'
      - Ambos preservam a estrutura de subpastas do prefixo monitorado

    Ao final, se houver qualquer falha, a tarefa é marcada como FAILED
    e os detalhes são publicados no XCom para a tarefa de notificação.
    """
    ti = context["ti"]
    params = context["params"]

    bucket = params.get("bucket", _DEFAULT_BUCKET)
    prefixo_monitorado = params.get("prefixo_monitorado", _DEFAULT_PREFIX)
    prefixo_processados = params.get("prefixo_processados", _DEFAULT_PROCESSED)
    prefixo_erros = params.get("prefixo_erros", _DEFAULT_ERRORS)
    aws_conn_id = params.get("aws_conn_id", _DEFAULT_CONN)
    max_paralelo = int(params.get("max_paralelo", 0))

    # Data de execução da DAG run: usada para particionar o destino dos processados.
    # Usa logical_date (Airflow 2.2+) com fallback para execution_date.
    data_dt = context.get("logical_date") or context["execution_date"]
    data_execucao = data_dt.strftime("%Y/%m/%d")  # ex: "2024/01/15"
    log.info("Data de particionamento: %s", data_execucao)

    arquivos: List[str] = ti.xcom_pull(
        task_ids="monitorar_s3", key="arquivos_encontrados"
    )

    if not arquivos:
        raise ValueError("Sensor não retornou arquivos para processar.")

    modo = f"paralelo ({max_paralelo} workers)" if max_paralelo > 0 else "sequencial"
    log.info("Modo: %s | Total de arquivos: %d", modo, len(arquivos))

    kwargs = dict(
        bucket=bucket,
        aws_conn_id=aws_conn_id,
        prefixo_origem=prefixo_monitorado,
        prefixo_processados=prefixo_processados,
        prefixo_erros=prefixo_erros,
        data_execucao=data_execucao,
    )

    resultados: List[Dict] = []

    if max_paralelo > 0:
        # ── Modo paralelo: ThreadPoolExecutor ─────────────────────────────
        # Cada arquivo é processado em uma thread separada.
        # O número máximo de threads simultâneas é controlado por max_paralelo.
        with ThreadPoolExecutor(max_workers=max_paralelo) as executor:
            futures = {
                executor.submit(_processar_um_arquivo, chave, **kwargs): chave
                for chave in arquivos
            }
            for future in as_completed(futures):
                resultados.append(future.result())
    else:
        # ── Modo sequencial: um arquivo de cada vez ───────────────────────
        # A ordem de processamento é a mesma ordem retornada pelo sensor.
        for chave in arquivos:
            resultado = _processar_um_arquivo(chave, **kwargs)
            resultados.append(resultado)

    # Classifica os resultados
    sucessos = [r for r in resultados if r["status"] == "sucesso"]
    falhas = [r for r in resultados if r["status"] == "falha"]

    log.info(
        "Resultado final: %d sucesso(s) | %d falha(s) | %d total",
        len(sucessos), len(falhas), len(resultados),
    )

    # Publica os resultados no XCom para uso pela tarefa de notificação
    ti.xcom_push(key="resultados", value=resultados)
    ti.xcom_push(key="falhas", value=falhas)

    if falhas:
        arquivos_com_falha = [f["chave"] for f in falhas]
        raise AirflowException(
            f"{len(falhas)} de {len(arquivos)} arquivo(s) falharam. "
            f"Verifique o prefixo '{prefixo_erros}' no bucket '{bucket}'. "
            f"Arquivos: {arquivos_com_falha}"
        )


def notificar_falha(**context) -> None:
    """
    Registra os detalhes da falha e aciona o canal de notificação.

    Executada somente quando 'processar_arquivos' falha (trigger_rule=one_failed).
    Lê os detalhes das falhas via XCom para incluir na notificação.

    ╔══════════════════════════════════════════════════════════════════╗
    ║  CUSTOMIZE AQUI: adicione o canal de notificação desejado.      ║
    ╚══════════════════════════════════════════════════════════════════╝
    """
    ti = context["ti"]
    dag_run = context.get("dag_run")

    falhas: List[Dict] = (
        ti.xcom_pull(task_ids="processar_arquivos", key="falhas") or []
    )

    detalhes = "\n".join(
        f"  - {f['chave']}: {f.get('erro', 'erro desconhecido')}" for f in falhas
    )

    log.error(
        "═══════════════════════════════════════════════════\n"
        "  FALHA NO PROCESSAMENTO S3\n"
        "  DAG    : %s\n"
        "  Run ID : %s\n"
        "  Falhas : %d arquivo(s)\n"
        "%s\n"
        "═══════════════════════════════════════════════════",
        dag_run.dag_id if dag_run else "N/A",
        dag_run.run_id if dag_run else "N/A",
        len(falhas),
        detalhes,
    )

    # ──────────────────────────────────────────────────────────────────
    # INÍCIO DAS NOTIFICAÇÕES — adicione o(s) canal(is) desejado(s)
    #
    # ► Slack (requer provider: apache-airflow-providers-slack)
    #   from airflow.providers.slack.hooks.slack_webhook import SlackWebhookHook
    #   mensagem = (
    #       f":x: *Falha no processamento S3*\n"
    #       f"DAG: `{dag_run.dag_id}` | Run: `{dag_run.run_id}`\n"
    #       f"Arquivos com falha:\n{detalhes}"
    #   )
    #   SlackWebhookHook(slack_webhook_conn_id="slack_default").send(text=mensagem)
    #
    # ► E-mail (configure smtp_* no airflow.cfg ou via env)
    #   from airflow.utils.email import send_email
    #   send_email(
    #       to=["equipe@empresa.com"],
    #       subject=f"[Airflow] Falha S3: {dag_run.dag_id}",
    #       html_content=f"<pre>{detalhes}</pre>",
    #   )
    #
    # ► SNS (requer provider: apache-airflow-providers-amazon)
    #   from airflow.providers.amazon.aws.hooks.sns import SnsHook
    #   SnsHook(aws_conn_id="aws_default").publish_to_target(
    #       target_arn="arn:aws:sns:us-east-1:123456789:meu-topico",
    #       message=f"Falha na DAG {dag_run.dag_id}:\n{detalhes}",
    #   )
    # ──────────────────────────────────────────────────────────────────


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
    description="Monitora prefixo S3, processa arquivos (sequencial ou paralelo) e move conforme resultado",
    schedule_interval=timedelta(minutes=5),
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
        # 0 = sequencial (um arquivo de cada vez)
        # N = paralelo (N arquivos simultâneos)
        "max_paralelo": 0,
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
            "Aguarda novos arquivos no prefixo S3 configurado (recursivo — inclui subpastas). "
            "Quando encontrados, publica as chaves completas no XCom e libera o fluxo."
        ),
    )

    # ── 2. Processa os arquivos e move cada um conforme resultado ────────
    processar = PythonOperator(
        task_id="processar_arquivos",
        python_callable=processar_arquivos,
        provide_context=True,
        doc_md=(
            "Processa cada arquivo individualmente (sequencial ou paralelo via 'max_paralelo'). "
            "Sucesso → moved para 'processados/'. Falha → movido para 'erros/'. "
            "Preserva estrutura de subpastas. Falha parcial é suportada."
        ),
    )

    # ── 3. Notificação (executada somente se houver falha) ────────────────
    notificar = PythonOperator(
        task_id="notificar_falha",
        python_callable=notificar_falha,
        provide_context=True,
        trigger_rule="one_failed",  # Só executa se 'processar_arquivos' falhar
        doc_md=(
            "Envia notificação detalhada com os arquivos que falharam. "
            "Configure o canal desejado (Slack, e-mail, SNS) na função notificar_falha()."
        ),
    )

    # ─────────────────────────────────────────────────────────────────────
    # Fluxo:
    #   monitorar_s3 → processar_arquivos → notificar_falha (só se falhar)
    # ─────────────────────────────────────────────────────────────────────
    monitorar_s3 >> processar >> notificar
