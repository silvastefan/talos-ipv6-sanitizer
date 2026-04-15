"""
DAG: s3_monitor_e_processar
============================

Visão Geral
-----------
Monitora automaticamente um prefixo (pasta) em um bucket AWS S3 e, ao detectar
novos arquivos — na raiz do prefixo ou dentro de subpastas —, processa cada um
de forma independente e o encaminha para a pasta correta conforme o resultado.

Esta DAG foi projetada para ser genérica: a lógica de negócio é um bloco de
código claramente demarcado que a equipe deve substituir pela transformação,
validação ou carga específica de cada projeto.

Fluxo de execução
-----------------

    ┌──────────────────┐     ┌──────────────────────┐     ┌────────────────────┐
    │  monitorar_s3    │────▶│  processar_arquivos   │────▶│  notificar_falha   │
    │  (Sensor S3)     │     │  (PythonOperator)     │     │  (só se falhar)    │
    └──────────────────┘     └──────────────────────┘     └────────────────────┘

    Tarefa 1 — monitorar_s3 (S3NovosArquivosSensor)
      • Verifica o prefixo S3 a cada 30 segundos (modo reschedule)
      • Listagem recursiva: detecta arquivos na raiz e em subpastas
      • Filtra por padrão de nome (glob), se configurado
      • Ao encontrar arquivos, publica as chaves no XCom e libera o fluxo
      • Se nenhum arquivo chegar em 4 horas → tarefa SKIPPED (não FAILED)

    Tarefa 2 — processar_arquivos (PythonOperator)
      • Obtém a lista de arquivos do XCom do sensor
      • Suporta dois modos de execução (parâmetro max_paralelo):
          - Sequencial (0): um arquivo de cada vez, ordem preservada
          - Paralelo (N): até N arquivos simultâneos via ThreadPoolExecutor
      • Cada arquivo é processado e movido de forma independente:
          - SUCESSO → processed/AAAA/MM/DD/[subpasta/]arquivo  (com data)
          - FALHA   → errors/[subpasta/]arquivo                (sem data)
      • Falha parcial é suportada: arquivos com sucesso não são bloqueados
        pela falha de outros arquivos do mesmo lote
      • Ao final, se houve qualquer falha, levanta AirflowException e
        publica os detalhes no XCom para a tarefa de notificação

    Tarefa 3 — notificar_falha (PythonOperator)
      • Executada SOMENTE quando processar_arquivos falha
      • Lê a lista detalhada de falhas do XCom
      • Envia notificação pelo canal configurado (Slack, e-mail, SNS)

Estrutura de pastas no S3
--------------------------

    bucket/
    ├── incoming/                       ← prefixo monitorado
    │   ├── relatorio.csv               ← arquivo na raiz
    │   └── vendas/                     ← subpasta
    │       └── jan_2024.parquet        ← arquivo em subpasta
    │
    ├── processed/                      ← destino dos arquivos com SUCESSO
    │   └── 2024/
    │       └── 01/
    │           └── 15/
    │               ├── relatorio.csv
    │               └── vendas/
    │                   └── jan_2024.parquet
    │
    └── errors/                         ← destino dos arquivos com FALHA
        ├── relatorio.csv
        └── vendas/
            └── jan_2024.parquet

    Observações sobre as pastas de destino:
      • A data usada é o logical_date da DAG run (não datetime.now), garantindo
        que reexecuções e backfills usem a data de agendamento correta.
      • A estrutura de subpastas é sempre preservada em ambos os destinos.
      • Erros não recebem partição por data para facilitar a localização e
        reprocessamento manual.

Configuração via Airflow Variables (Admin → Variables)
-------------------------------------------------------
    Variável                Padrão          Descrição
    ─────────────────────   ───────────     ──────────────────────────────────────
    s3_bucket               meu-bucket      Nome do bucket S3
    s3_monitor_prefix       incoming/       Prefixo a monitorar (com '/' no final)
    s3_processed_prefix     processed/      Prefixo dos arquivos com sucesso
    s3_error_prefix         errors/         Prefixo dos arquivos com falha
    s3_file_pattern         *               Filtro glob de nome (ex: *.csv)
    aws_conn_id             aws_default     ID da conexão AWS no Airflow

    As variáveis são lidas na inicialização do DAG. Para alterá-las sem reiniciar
    o scheduler, use os parâmetros de execução (ver abaixo).

Parâmetros por execução (Trigger DAG w/ config)
------------------------------------------------
    Os mesmos campos, mais max_paralelo, podem ser passados como JSON ao
    disparar a DAG manualmente, sobrescrevendo as Variables para aquela execução:

    {
        "bucket":             "meu-bucket",
        "prefixo_monitorado": "incoming/",
        "prefixo_processados":"processed/",
        "prefixo_erros":      "errors/",
        "padrao_arquivo":     "*.csv",
        "aws_conn_id":        "aws_default",
        "max_paralelo":       0
    }

Pontos de customização (marcados no código com ╔═╗)
---------------------------------------------------
    1. Lógica de negócio  → função _processar_um_arquivo(), bloco marcado
    2. Canal de notificação → função notificar_falha(), bloco marcado

Dependências
------------
    apache-airflow-providers-amazon  (S3Hook)
    (ver requirements-airflow.txt para lista completa)

Permissões IAM necessárias
--------------------------
    s3:ListBucket          no bucket
    s3:GetObject           nos objetos do prefixo monitorado
    s3:PutObject           nos prefixos processed/ e errors/
    s3:DeleteObject        no prefixo monitorado (para "mover" = copiar + deletar)
    s3:CopyObject         (implícito via s3:PutObject no destino)
"""

import fnmatch
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Dict, List, Optional

from airflow import DAG
from airflow.exceptions import AirflowException
from airflow.models import Variable
from airflow.operators.python import PythonOperator
from airflow.sensors.base import BaseSensorOperator
from airflow.utils.dates import days_ago

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Valores padrão — lidos das Airflow Variables na inicialização do DAG.
# Podem ser sobrescritos por parâmetros de execução (Trigger DAG w/ config).
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_BUCKET    = Variable.get("s3_bucket",            default_var="meu-bucket")
_DEFAULT_PREFIX    = Variable.get("s3_monitor_prefix",    default_var="incoming/")
_DEFAULT_PROCESSED = Variable.get("s3_processed_prefix",  default_var="processed/")
_DEFAULT_ERRORS    = Variable.get("s3_error_prefix",      default_var="errors/")
_DEFAULT_PATTERN   = Variable.get("s3_file_pattern",      default_var="*")
_DEFAULT_CONN      = Variable.get("aws_conn_id",          default_var="aws_default")


# ─────────────────────────────────────────────────────────────────────────────
# TAREFA 1 — Sensor customizado
# ─────────────────────────────────────────────────────────────────────────────
class S3NovosArquivosSensor(BaseSensorOperator):
    """
    Sensor que verifica periodicamente se há arquivos em um prefixo S3.

    Estende BaseSensorOperator com lógica específica para listar objetos S3
    de forma recursiva e aplicar filtro de nome (glob).

    Comportamento
    -------------
    • A cada poke_interval segundos, chama poke() que lista o prefixo no S3.
    • A listagem é recursiva: detecta arquivos na raiz DO prefixo E dentro
      de subpastas em qualquer nível de profundidade.
    • Quando encontra arquivos correspondentes, publica a lista completa de
      chaves S3 no XCom sob a chave 'arquivos_encontrados' e retorna True,
      liberando as tarefas dependentes.
    • Quando não encontra arquivos, retorna False e aguarda o próximo poke.
    • Em modo 'reschedule': o worker é liberado entre os pokes, economizando
      recursos do Airflow.
    • Em modo 'soft_fail=True': ao atingir o timeout, a tarefa é marcada
      como SKIPPED em vez de FAILED, evitando alertas desnecessários.

    Parâmetros
    ----------
    bucket_name : str
        Nome do bucket S3 a ser monitorado.
        Exemplo: 'minha-empresa-dados'
    prefix : str
        Prefixo (pasta) a ser monitorado. Deve terminar com '/'.
        Exemplo: 'incoming/' ou 'dados/entrada/diario/'
    file_pattern : str, opcional
        Filtro de nome de arquivo no formato glob (padrão: '*' = todos).
        Aplicado apenas ao nome do arquivo, não ao caminho completo.
        Exemplos: '*.csv', 'relatorio_*.xlsx', 'dados_????.json'
    aws_conn_id : str, opcional
        ID da conexão AWS configurada no Airflow (Admin → Connections).
        Padrão: 'aws_default'

    XCom publicado
    --------------
    Chave: 'arquivos_encontrados'
    Valor: List[str] — lista de chaves S3 completas dos arquivos encontrados.
    Exemplo: ['incoming/arquivo.csv', 'incoming/subpasta/dados.json']

    Exemplo de uso
    --------------
    sensor = S3NovosArquivosSensor(
        task_id="monitorar_s3",
        bucket_name="minha-empresa-dados",
        prefix="incoming/",
        file_pattern="*.csv",
        aws_conn_id="aws_producao",
        poke_interval=60,       # verifica a cada 60 segundos
        timeout=60 * 60 * 8,   # timeout após 8 horas
        mode="reschedule",
        soft_fail=True,
    )
    """

    # Campos que suportam templates Jinja (ex: "{{ params.bucket }}")
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
        """
        Executa uma verificação no S3. Chamado automaticamente pelo Airflow
        a cada poke_interval segundos.

        Parâmetros
        ----------
        context : dict
            Contexto de execução do Airflow (contém 'ti', 'dag_run', etc.).

        Retorna
        -------
        bool
            True  → arquivos encontrados; lista publicada no XCom; fluxo liberado.
            False → nenhum arquivo; sensor aguarda o próximo poke.
        """
        from airflow.providers.amazon.aws.hooks.s3 import S3Hook

        hook = S3Hook(aws_conn_id=self.aws_conn_id)

        self.log.info(
            "Verificando s3://%s/%s | padrão de arquivo: '%s'",
            self.bucket_name,
            self.prefix,
            self.file_pattern,
        )

        # list_keys() é recursivo: retorna TODOS os objetos sob o prefixo,
        # incluindo arquivos dentro de subpastas em qualquer profundidade.
        # Retorna None se o bucket/prefixo não existir; usamos "or []" como fallback.
        chaves = hook.list_keys(bucket_name=self.bucket_name, prefix=self.prefix) or []

        # Alguns buckets S3 criam objetos "vazios" terminados em '/' para simular
        # pastas. Filtramos esses marcadores, pois não são arquivos reais.
        arquivos = [k for k in chaves if not k.endswith("/")]

        # Aplica o filtro de nome (glob) somente sobre o nome do arquivo
        # (última parte do caminho), ignorando o caminho de subpastas.
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
            self.log.info("  • s3://%s/%s", self.bucket_name, a)

        # Publica a lista no XCom para que processar_arquivos possa consumi-la.
        # O XCom é armazenado no banco de metadados do Airflow.
        context["ti"].xcom_push(key="arquivos_encontrados", value=arquivos)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Funções auxiliares — cálculo de caminhos S3
# ─────────────────────────────────────────────────────────────────────────────
def _calcular_destino_processados(
    chave: str,
    prefixo_origem: str,
    prefixo_processados: str,
    data_execucao: str,
) -> str:
    """
    Calcula o caminho de destino para um arquivo processado com SUCESSO.

    O destino inclui uma partição por data no formato AAAA/MM/DD, baseada
    na data de execução lógica da DAG run. Isso permite consultas particionadas
    (ex: AWS Athena, Glue) e facilita auditorias por período.

    A estrutura de subpastas relativa ao prefixo monitorado é preservada,
    possibilitando rastrear a origem do arquivo.

    Parâmetros
    ----------
    chave : str
        Chave S3 completa do arquivo de origem.
        Exemplo: 'incoming/vendas/jan_2024.csv'
    prefixo_origem : str
        Prefixo monitorado (prefixo da DAG). Exemplo: 'incoming/'
    prefixo_processados : str
        Prefixo de destino dos processados. Exemplo: 'processed/'
    data_execucao : str
        Data no formato 'AAAA/MM/DD'. Exemplo: '2024/01/15'

    Retorna
    -------
    str
        Chave S3 de destino com partição de data.

    Exemplos
    --------
    >>> _calcular_destino_processados(
    ...     "incoming/arquivo.csv", "incoming/", "processed/", "2024/01/15"
    ... )
    'processed/2024/01/15/arquivo.csv'

    >>> _calcular_destino_processados(
    ...     "incoming/vendas/jan.parquet", "incoming/", "processed/", "2024/01/15"
    ... )
    'processed/2024/01/15/vendas/jan.parquet'
    """
    # Remove o prefixo de origem para obter apenas o caminho relativo.
    # Exemplo: 'incoming/vendas/jan.parquet' → 'vendas/jan.parquet'
    caminho_relativo = chave[len(prefixo_origem):]

    # Monta o destino: prefixo + data + caminho relativo
    return f"{prefixo_processados.rstrip('/')}/{data_execucao}/{caminho_relativo}"


def _calcular_destino_erros(
    chave: str,
    prefixo_origem: str,
    prefixo_erros: str,
) -> str:
    """
    Calcula o caminho de destino para um arquivo que FALHOU no processamento.

    Não inclui partição por data, facilitando a localização dos arquivos
    problemáticos para reprocessamento ou análise manual.

    A estrutura de subpastas relativa ao prefixo monitorado é preservada.

    Parâmetros
    ----------
    chave : str
        Chave S3 completa do arquivo de origem.
        Exemplo: 'incoming/vendas/jan_2024.csv'
    prefixo_origem : str
        Prefixo monitorado. Exemplo: 'incoming/'
    prefixo_erros : str
        Prefixo de destino dos erros. Exemplo: 'errors/'

    Retorna
    -------
    str
        Chave S3 de destino na pasta de erros.

    Exemplos
    --------
    >>> _calcular_destino_erros("incoming/arquivo.csv", "incoming/", "errors/")
    'errors/arquivo.csv'

    >>> _calcular_destino_erros("incoming/vendas/jan.parquet", "incoming/", "errors/")
    'errors/vendas/jan.parquet'
    """
    caminho_relativo = chave[len(prefixo_origem):]
    return f"{prefixo_erros.rstrip('/')}/{caminho_relativo}"


def _mover_arquivo_s3(hook, bucket: str, origem: str, destino: str) -> None:
    """
    Move um objeto S3 do caminho de origem para o de destino.

    O S3 não possui operação nativa de 'mover'. A operação é implementada
    como duas chamadas atômicas sequenciais:
      1. CopyObject: cria uma cópia no destino
      2. DeleteObject: remove o original

    Atenção: se DeleteObject falhar após CopyObject ter sido bem-sucedido,
    o arquivo existirá nos dois caminhos. Isso é improvável, mas possível
    em situações de rede instável. Nesse caso, a próxima execução do sensor
    detectará o arquivo ainda no prefixo monitorado e tentará reprocessá-lo.

    Parâmetros
    ----------
    hook : S3Hook
        Instância do hook S3 autenticado.
    bucket : str
        Nome do bucket S3 (origem e destino são no mesmo bucket).
    origem : str
        Chave S3 do objeto a ser movido.
    destino : str
        Chave S3 de destino.

    Levanta
    -------
    botocore.exceptions.ClientError
        Se a operação de cópia ou exclusão falhar no lado da AWS.
    """
    s3 = hook.get_conn()  # Retorna o cliente boto3 autenticado

    # Passo 1: copia o objeto para o destino
    s3.copy_object(
        CopySource={"Bucket": bucket, "Key": origem},
        Bucket=bucket,
        Key=destino,
    )

    # Passo 2: remove o original (somente após a cópia ser confirmada)
    s3.delete_object(Bucket=bucket, Key=origem)

    log.info("Movido: s3://%s/%s  →  s3://%s/%s", bucket, origem, bucket, destino)


# ─────────────────────────────────────────────────────────────────────────────
# TAREFA 2 (núcleo) — Processamento de um único arquivo
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
    Processa um único arquivo S3 e o move para o destino adequado.

    Esta função é o núcleo da DAG. Ela:
      1. Lê o arquivo do S3
      2. Executa a lógica de negócio (bloco customizável)
      3. Em caso de SUCESSO: move para processed/AAAA/MM/DD/
      4. Em caso de FALHA:   move para errors/ e registra o erro

    Thread-safety
    -------------
    Cada chamada cria seu próprio S3Hook (e portanto seu próprio cliente boto3),
    tornando a função segura para execução em paralelo via ThreadPoolExecutor
    sem compartilhar estado entre threads.

    Parâmetros
    ----------
    chave : str
        Chave S3 completa do arquivo a processar.
        Exemplo: 'incoming/vendas/jan_2024.csv'
    bucket : str
        Nome do bucket S3.
    aws_conn_id : str
        ID da conexão AWS configurada no Airflow.
    prefixo_origem : str
        Prefixo monitorado. Usado para calcular o caminho relativo.
    prefixo_processados : str
        Prefixo de destino para arquivos com sucesso.
    prefixo_erros : str
        Prefixo de destino para arquivos com falha.
    data_execucao : str
        Data de execução no formato 'AAAA/MM/DD'. Usada na partição de destino.

    Retorna
    -------
    dict
        Dicionário com o resultado do processamento:
        {
            "chave":   str,              # chave S3 original do arquivo
            "status":  "sucesso"|"falha",
            "destino": str,              # chave S3 para onde foi movido
            "erro":    str               # mensagem de erro (apenas em falha)
        }

    ╔══════════════════════════════════════════════════════════════════╗
    ║  CUSTOMIZE AQUI: substitua o bloco de lógica de negócio.        ║
    ║  O bloco está claramente demarcado com comentários de início     ║
    ║  e fim. NÃO altere o código fora desse bloco.                   ║
    ╚══════════════════════════════════════════════════════════════════╝
    """
    from airflow.providers.amazon.aws.hooks.s3 import S3Hook

    # Cria hook próprio por chamada — necessário para thread-safety no modo paralelo.
    # No modo sequencial, o overhead é mínimo.
    hook = S3Hook(aws_conn_id=aws_conn_id)

    log.info("── Iniciando processamento: s3://%s/%s", bucket, chave)

    try:
        # ══════════════════════════════════════════════════════════════════
        # INÍCIO DA LÓGICA DE NEGÓCIO
        #
        # Leia o arquivo e aplique a transformação, validação ou carga
        # necessária para o seu caso de uso.
        #
        # O objeto S3 é obtido via hook para aproveitar a autenticação
        # já configurada na conexão do Airflow.
        #
        # ── Exemplo 1: leitura de CSV com pandas ──────────────────────
        #   import pandas as pd, io
        #   corpo = hook.get_key(chave, bucket_name=bucket).get()["Body"].read()
        #   df = pd.read_csv(io.BytesIO(corpo))
        #   # ... processa o DataFrame ...
        #   # Para gravar de volta no S3:
        #   saida = io.BytesIO()
        #   df.to_parquet(saida)
        #   hook.load_bytes(saida.getvalue(), key=chave_destino, bucket_name=bucket)
        #
        # ── Exemplo 2: chamada a uma API externa ──────────────────────
        #   import requests
        #   payload = {"bucket": bucket, "chave": chave}
        #   resposta = requests.post("https://api.empresa.com/processar", json=payload)
        #   resposta.raise_for_status()  # levanta exceção em caso de erro HTTP
        #
        # ── Exemplo 3: invocação de Lambda AWS ────────────────────────
        #   import boto3, json
        #   lam = boto3.client("lambda", region_name="us-east-1")
        #   response = lam.invoke(
        #       FunctionName="minha-funcao-processamento",
        #       InvocationType="RequestResponse",
        #       Payload=json.dumps({"bucket": bucket, "chave": chave}),
        #   )
        #   if response["StatusCode"] != 200:
        #       raise RuntimeError(f"Lambda retornou status {response['StatusCode']}")
        #
        # ── Exemplo 4: carga em banco de dados ────────────────────────
        #   from airflow.providers.postgres.hooks.postgres import PostgresHook
        #   import pandas as pd, io
        #   corpo = hook.get_key(chave, bucket_name=bucket).get()["Body"].read()
        #   df = pd.read_csv(io.BytesIO(corpo))
        #   pg = PostgresHook(postgres_conn_id="postgres_default")
        #   pg.insert_rows(table="minha_tabela", rows=df.values.tolist())
        # ══════════════════════════════════════════════════════════════════
        obj = hook.get_key(chave, bucket_name=bucket)
        conteudo = obj.get()["Body"].read()
        log.info(
            "Arquivo lido com sucesso (%d bytes). Executando lógica de negócio...",
            len(conteudo),
        )
        # FIM DA LÓGICA DE NEGÓCIO ════════════════════════════════════

        # SUCESSO: calcula destino com partição de data e move o arquivo
        destino = _calcular_destino_processados(
            chave, prefixo_origem, prefixo_processados, data_execucao
        )
        _mover_arquivo_s3(hook, bucket, chave, destino)

        log.info("── Concluído com SUCESSO: %s  →  %s", chave, destino)
        return {"chave": chave, "status": "sucesso", "destino": destino}

    except Exception as exc:
        # Qualquer exceção não tratada dentro do bloco de lógica de negócio
        # (ou nas operações S3) é capturada aqui.
        log.error("── FALHA ao processar '%s': %s", chave, exc)

        # FALHA: calcula destino na pasta de erros e tenta mover o arquivo
        destino_erro = _calcular_destino_erros(chave, prefixo_origem, prefixo_erros)
        try:
            _mover_arquivo_s3(hook, bucket, chave, destino_erro)
        except Exception as move_exc:
            # Cenário extremo: não foi possível nem mover para a pasta de erros.
            # O arquivo permanece no local original. O log registra o ocorrido
            # para que a equipe possa agir manualmente.
            log.error(
                "Não foi possível mover '%s' para erros ('%s'): %s. "
                "O arquivo PERMANECE em s3://%s/%s.",
                chave, destino_erro, move_exc, bucket, chave,
            )
            destino_erro = chave  # registra que ficou no lugar original

        log.info("── Arquivo movido para erros: %s  →  %s", chave, destino_erro)

        return {
            "chave": chave,
            "status": "falha",
            "destino": destino_erro,
            "erro": str(exc),
        }


# ─────────────────────────────────────────────────────────────────────────────
# TAREFA 2 — Orquestrador do processamento
# ─────────────────────────────────────────────────────────────────────────────
def processar_arquivos(**context) -> None:
    """
    Orquestra o processamento de todos os arquivos detectados pelo sensor.

    Obtém a lista de arquivos do XCom do sensor, distribui o processamento
    (sequencial ou paralelo) e consolida os resultados. Ao final, se houver
    qualquer falha, publica os detalhes no XCom e levanta AirflowException
    para acionar a tarefa de notificação.

    Modo sequencial (max_paralelo = 0)
    ------------------------------------
    Processa um arquivo de cada vez na ordem retornada pelo sensor.
    Use quando:
      • A ordem de processamento importa
      • Os arquivos compartilham recursos (ex: mesma tabela DB sem lock)
      • A lógica de negócio não é thread-safe

    Modo paralelo (max_paralelo = N)
    ----------------------------------
    Processa até N arquivos simultaneamente via ThreadPoolExecutor.
    Use quando:
      • Os arquivos são independentes entre si
      • O processamento envolve I/O intensivo (S3, APIs, banco)
      • Velocidade é prioritária e a lógica é thread-safe

    Parâmetros (via context["params"])
    -----------------------------------
    bucket : str
        Nome do bucket S3.
    prefixo_monitorado : str
        Prefixo de origem dos arquivos.
    prefixo_processados : str
        Prefixo de destino para sucesso.
    prefixo_erros : str
        Prefixo de destino para falhas.
    aws_conn_id : str
        ID da conexão AWS no Airflow.
    max_paralelo : int
        0 = sequencial; N > 0 = N threads paralelas.

    XCom publicado
    --------------
    Chave 'resultados': List[Dict] com todos os arquivos e seus resultados.
    Chave 'falhas':     List[Dict] apenas com os arquivos que falharam
                        (lida pela tarefa notificar_falha).

    Levanta
    -------
    ValueError
        Se o sensor não publicou arquivos no XCom (não deveria ocorrer em uso normal).
    AirflowException
        Se um ou mais arquivos falharam no processamento. Aciona notificar_falha.
    """
    ti = context["ti"]
    params = context["params"]

    # Lê parâmetros da execução, com fallback para as Airflow Variables
    bucket             = params.get("bucket",             _DEFAULT_BUCKET)
    prefixo_monitorado = params.get("prefixo_monitorado", _DEFAULT_PREFIX)
    prefixo_processados = params.get("prefixo_processados", _DEFAULT_PROCESSED)
    prefixo_erros      = params.get("prefixo_erros",      _DEFAULT_ERRORS)
    aws_conn_id        = params.get("aws_conn_id",        _DEFAULT_CONN)
    max_paralelo       = int(params.get("max_paralelo",   0))

    # Extrai a data de execução lógica da DAG run para particionamento.
    # logical_date é a data de agendamento (Airflow 2.2+); execution_date é
    # o equivalente em versões anteriores. Usando o logical_date garantimos
    # que reprocessamentos e backfills usem a data de agendamento correta,
    # não o momento em que o DAG está sendo executado.
    data_dt = context.get("logical_date") or context["execution_date"]
    data_execucao = data_dt.strftime("%Y/%m/%d")  # ex: "2024/01/15"
    log.info("Data de particionamento (logical_date): %s", data_execucao)

    # Recupera a lista de arquivos publicada pelo sensor no XCom
    arquivos: List[str] = ti.xcom_pull(
        task_ids="monitorar_s3", key="arquivos_encontrados"
    )

    if not arquivos:
        raise ValueError(
            "A tarefa 'monitorar_s3' não publicou arquivos no XCom. "
            "Verifique se o sensor foi executado corretamente."
        )

    modo = f"paralelo ({max_paralelo} workers)" if max_paralelo > 0 else "sequencial"
    log.info(
        "Iniciando processamento | Modo: %s | Arquivos: %d | Data: %s",
        modo, len(arquivos), data_execucao,
    )

    # Argumentos comuns passados para cada chamada de _processar_um_arquivo
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
        # ── Modo paralelo ─────────────────────────────────────────────────
        # ThreadPoolExecutor cria um pool de threads reutilizáveis.
        # 'as_completed' coleta os resultados na ordem de conclusão
        # (não na ordem de submissão), o que é aceitável aqui pois
        # cada arquivo é independente.
        log.info("Iniciando ThreadPoolExecutor com %d worker(s)...", max_paralelo)
        with ThreadPoolExecutor(max_workers=max_paralelo) as executor:
            futures = {
                executor.submit(_processar_um_arquivo, chave, **kwargs): chave
                for chave in arquivos
            }
            for future in as_completed(futures):
                # future.result() levanta a exceção original se a thread falhou.
                # No nosso caso, _processar_um_arquivo captura todas as exceções
                # internamente e retorna um dict com status="falha", portanto
                # future.result() nunca levanta exceção aqui.
                resultados.append(future.result())
    else:
        # ── Modo sequencial ───────────────────────────────────────────────
        # Processa um arquivo de cada vez na ordem da lista.
        # O próximo arquivo só começa após o anterior ser concluído (e movido).
        for chave in arquivos:
            resultado = _processar_um_arquivo(chave, **kwargs)
            resultados.append(resultado)

    # Classifica os resultados para relatório e para o XCom de notificação
    sucessos = [r for r in resultados if r["status"] == "sucesso"]
    falhas   = [r for r in resultados if r["status"] == "falha"]

    log.info(
        "Resultado final: %d sucesso(s) | %d falha(s) | %d total",
        len(sucessos), len(falhas), len(resultados),
    )

    # Publica resultados completos e lista de falhas no XCom.
    # A tarefa notificar_falha lê a chave 'falhas' para compor a mensagem.
    ti.xcom_push(key="resultados", value=resultados)
    ti.xcom_push(key="falhas",     value=falhas)

    if falhas:
        arquivos_com_falha = [f["chave"] for f in falhas]
        # Levanta AirflowException para:
        #   1. Marcar esta tarefa como FAILED no Airflow
        #   2. Acionar a tarefa notificar_falha (trigger_rule=one_failed)
        #   3. Acionar o _on_failure_callback
        raise AirflowException(
            f"{len(falhas)} de {len(arquivos)} arquivo(s) falharam no processamento. "
            f"Verifique a pasta '{prefixo_erros}' no bucket '{bucket}'. "
            f"Arquivos com falha: {arquivos_com_falha}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TAREFA 3 — Notificação de falha
# ─────────────────────────────────────────────────────────────────────────────
def notificar_falha(**context) -> None:
    """
    Envia notificação detalhada quando o processamento falha.

    Esta tarefa é executada SOMENTE quando a tarefa processar_arquivos falha
    (trigger_rule='one_failed'). Ela lê a lista de falhas publicada no XCom
    e envia uma notificação pelo canal configurado.

    O log sempre é gravado independentemente do canal de notificação.
    Configure o canal desejado (Slack, e-mail ou SNS) no bloco marcado.

    Parâmetros (via context)
    ------------------------
    context["ti"] : TaskInstance
        Usado para ler o XCom 'falhas' publicado por processar_arquivos.
    context["dag_run"] : DagRun
        Usado para obter dag_id e run_id para a mensagem de notificação.

    XCom consumido
    --------------
    Chave 'falhas' da tarefa 'processar_arquivos':
        List[Dict] com {"chave", "status", "destino", "erro"} de cada arquivo falho.

    ╔══════════════════════════════════════════════════════════════════╗
    ║  CUSTOMIZE AQUI: descomente o bloco do canal desejado.          ║
    ╚══════════════════════════════════════════════════════════════════╝
    """
    ti = context["ti"]
    dag_run = context.get("dag_run")

    # Recupera a lista de falhas publicada pela tarefa de processamento
    falhas: List[Dict] = (
        ti.xcom_pull(task_ids="processar_arquivos", key="falhas") or []
    )

    # Formata os detalhes de cada arquivo que falhou para a mensagem
    detalhes = "\n".join(
        f"  • {f['chave']}: {f.get('erro', 'erro desconhecido')}"
        for f in falhas
    )

    # Log estruturado sempre gravado (independente do canal de notificação)
    log.error(
        "═══════════════════════════════════════════════════════\n"
        "  FALHA NO PROCESSAMENTO S3\n"
        "  DAG    : %s\n"
        "  Run ID : %s\n"
        "  Falhas : %d arquivo(s)\n"
        "%s\n"
        "═══════════════════════════════════════════════════════",
        dag_run.dag_id if dag_run else "N/A",
        dag_run.run_id if dag_run else "N/A",
        len(falhas),
        detalhes,
    )

    # ══════════════════════════════════════════════════════════════════════
    # INÍCIO DOS CANAIS DE NOTIFICAÇÃO — descomente o(s) desejado(s)
    #
    # ── Opção A: Slack ────────────────────────────────────────────────
    # Pré-requisito: apache-airflow-providers-slack (requirements-airflow.txt)
    # Configuração:  Admin → Connections → slack_default (Conn Type: Slack Webhook)
    #
    #   from airflow.providers.slack.hooks.slack_webhook import SlackWebhookHook
    #   mensagem = (
    #       f":x: *Falha no processamento S3*\n"
    #       f"*DAG:*    `{dag_run.dag_id}`\n"
    #       f"*Run ID:* `{dag_run.run_id}`\n"
    #       f"*Arquivos com falha ({len(falhas)}):*\n{detalhes}"
    #   )
    #   SlackWebhookHook(slack_webhook_conn_id="slack_default").send(text=mensagem)
    #
    # ── Opção B: E-mail ───────────────────────────────────────────────
    # Pré-requisito: configurar smtp_host, smtp_port, etc. no airflow.cfg
    #               ou via variáveis de ambiente AIRFLOW__SMTP__*
    #
    #   from airflow.utils.email import send_email
    #   send_email(
    #       to=["equipe-dados@empresa.com"],
    #       subject=f"[Airflow] Falha no processamento S3 — {dag_run.dag_id}",
    #       html_content=(
    #           f"<h3>Falha no processamento S3</h3>"
    #           f"<p><b>DAG:</b> {dag_run.dag_id}</p>"
    #           f"<p><b>Run ID:</b> {dag_run.run_id}</p>"
    #           f"<p><b>Arquivos com falha ({len(falhas)}):</b></p>"
    #           f"<pre>{detalhes}</pre>"
    #       ),
    #   )
    #
    # ── Opção C: AWS SNS (Simple Notification Service) ────────────────
    # Pré-requisito: apache-airflow-providers-amazon (já incluso)
    # Configuração:  crie um tópico SNS e adicione as assinaturas desejadas
    #
    #   from airflow.providers.amazon.aws.hooks.sns import SnsHook
    #   SnsHook(aws_conn_id="aws_default").publish_to_target(
    #       target_arn="arn:aws:sns:us-east-1:123456789012:alerta-processamento",
    #       message=(
    #           f"Falha no processamento S3\n"
    #           f"DAG: {dag_run.dag_id}\n"
    #           f"Run ID: {dag_run.run_id}\n"
    #           f"Arquivos com falha ({len(falhas)}):\n{detalhes}"
    #       ),
    #       subject=f"[Airflow] Falha S3 — {dag_run.dag_id}",
    #   )
    # ══════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
# Callback global de falha — acionado em QUALQUER tarefa que falhe
# ─────────────────────────────────────────────────────────────────────────────
def _on_failure_callback(context: dict) -> None:
    """
    Callback executado automaticamente pelo Airflow quando qualquer tarefa falha.

    Definido em default_args, este callback é chamado antes da tarefa ser
    marcada como FAILED. É diferente de notificar_falha (que é uma tarefa
    explícita): o callback é sempre executado em qualquer falha, enquanto
    notificar_falha só é executada quando processar_arquivos falha.

    Este callback registra um log de erro estruturado. Pode ser expandido
    com lógica adicional se necessário.

    Parâmetros
    ----------
    context : dict
        Contexto de execução do Airflow. Chaves relevantes:
          - 'dag_run':        DagRun com dag_id e run_id
          - 'task_instance':  TaskInstance com task_id
          - 'exception':      A exceção que causou a falha
    """
    dag_run        = context.get("dag_run")
    task_instance  = context.get("task_instance")
    exception      = context.get("exception")

    log.error(
        "Falha detectada | DAG: %s | Run: %s | Tarefa: %s | Erro: %s",
        dag_run.dag_id       if dag_run       else "N/A",
        dag_run.run_id       if dag_run       else "N/A",
        task_instance.task_id if task_instance else "N/A",
        exception,
    )


# ─────────────────────────────────────────────────────────────────────────────
# DEFINIÇÃO DA DAG
# ─────────────────────────────────────────────────────────────────────────────
with DAG(
    # Identificador único da DAG no Airflow
    dag_id="s3_monitor_e_processar",

    description=(
        "Monitora um prefixo S3, processa os arquivos encontrados "
        "(sequencial ou paralelo) e move cada um para processed/ ou errors/ "
        "conforme o resultado, com partição por data em caso de sucesso."
    ),

    # Frequência de execução da DAG.
    # O sensor interno verifica o S3 a cada 30s dentro de cada execução.
    # Ajuste conforme a latência aceitável para detecção de novos arquivos.
    schedule_interval=timedelta(minutes=5),

    # Data de início das execuções agendadas
    start_date=days_ago(1),

    # Não executa execuções passadas ao ativar a DAG
    catchup=False,

    # Limita a uma execução simultânea. Isso é essencial para evitar que
    # duas execuções concorrentes detectem e processem os mesmos arquivos.
    max_active_runs=1,

    default_args={
        "owner": "airflow",
        "depends_on_past": False,   # execuções independentes entre si
        "retries": 1,               # uma tentativa extra em caso de falha transitória
        "retry_delay": timedelta(minutes=2),
        "on_failure_callback": _on_failure_callback,
    },

    # Parâmetros configuráveis por execução.
    # Valores padrão lidos das Airflow Variables; podem ser sobrescritos
    # ao disparar a DAG manualmente com "Trigger DAG w/ config".
    params={
        # Nome do bucket S3 a ser monitorado
        "bucket": _DEFAULT_BUCKET,

        # Prefixo (pasta) a ser monitorado — deve terminar com '/'
        "prefixo_monitorado": _DEFAULT_PREFIX,

        # Prefixo de destino dos arquivos processados com sucesso
        # O caminho final será: prefixo_processados/AAAA/MM/DD/[subpasta/]arquivo
        "prefixo_processados": _DEFAULT_PROCESSED,

        # Prefixo de destino dos arquivos que falharam no processamento
        # O caminho final será: prefixo_erros/[subpasta/]arquivo
        "prefixo_erros": _DEFAULT_ERRORS,

        # Filtro de nome de arquivo (glob). Exemplos: *.csv, dados_*.json, *
        "padrao_arquivo": _DEFAULT_PATTERN,

        # ID da conexão AWS configurada no Airflow (Admin → Connections)
        "aws_conn_id": _DEFAULT_CONN,

        # Modo de processamento:
        #   0 = sequencial: um arquivo de cada vez (padrão, mais seguro)
        #   N = paralelo: até N arquivos simultaneamente (mais rápido)
        "max_paralelo": 0,
    },

    tags=["s3", "monitoramento", "etl"],
) as dag:

    # ── Tarefa 1: Sensor S3 ───────────────────────────────────────────────
    # Verifica o prefixo S3 a cada 30 segundos em modo 'reschedule'
    # (o worker é liberado entre os pokes, economizando slots).
    # soft_fail=True garante que um timeout após 4h não quebre a DAG.
    monitorar_s3 = S3NovosArquivosSensor(
        task_id="monitorar_s3",

        # Suporta templates Jinja: os valores são resolvidos em runtime
        bucket_name="{{ params.bucket }}",
        prefix="{{ params.prefixo_monitorado }}",
        file_pattern="{{ params.padrao_arquivo }}",
        aws_conn_id="{{ params.aws_conn_id }}",

        poke_interval=30,         # verifica o S3 a cada 30 segundos
        timeout=60 * 60 * 4,     # timeout após 4 horas sem arquivo
        mode="reschedule",        # libera o worker entre os pokes
        soft_fail=True,           # timeout → SKIPPED, não FAILED

        doc_md=(
            "**Sensor S3 — monitorar_s3**\n\n"
            "Aguarda novos arquivos no prefixo S3 configurado.\n"
            "A listagem é recursiva e inclui arquivos em subpastas.\n"
            "Ao encontrar arquivos, publica as chaves no XCom e libera o fluxo.\n\n"
            "- Verificação a cada 30 segundos\n"
            "- Timeout após 4 horas (tarefa marcada como SKIPPED)\n"
            "- Não ocupa slot de worker entre verificações (modo reschedule)"
        ),
    )

    # ── Tarefa 2: Processamento e movimentação dos arquivos ───────────────
    # Para cada arquivo encontrado pelo sensor, executa a lógica de negócio
    # e move o arquivo para processed/ (sucesso) ou errors/ (falha).
    processar = PythonOperator(
        task_id="processar_arquivos",
        python_callable=processar_arquivos,
        provide_context=True,

        doc_md=(
            "**Processamento — processar_arquivos**\n\n"
            "Processa cada arquivo individualmente.\n\n"
            "**Modos de execução** (parâmetro `max_paralelo`):\n"
            "- `0` → sequencial: um arquivo de cada vez\n"
            "- `N` → paralelo: até N arquivos simultâneos\n\n"
            "**Destino em caso de sucesso:** `processed/AAAA/MM/DD/[subpasta/]arquivo`\n\n"
            "**Destino em caso de falha:** `errors/[subpasta/]arquivo`\n\n"
            "Falhas parciais são suportadas: arquivos com sucesso não são "
            "afetados pela falha de outros arquivos do mesmo lote."
        ),
    )

    # ── Tarefa 3: Notificação de falha ────────────────────────────────────
    # Executada SOMENTE quando processar_arquivos falha.
    # trigger_rule="one_failed": executa se pelo menos um upstream falhou.
    notificar = PythonOperator(
        task_id="notificar_falha",
        python_callable=notificar_falha,
        provide_context=True,
        trigger_rule="one_failed",

        doc_md=(
            "**Notificação — notificar_falha**\n\n"
            "Executada **somente quando** `processar_arquivos` falha.\n\n"
            "Lê a lista detalhada de arquivos com falha do XCom e envia "
            "uma notificação pelo canal configurado.\n\n"
            "**Canais disponíveis** (configure em `notificar_falha()`):\n"
            "- Slack\n- E-mail (SMTP)\n- AWS SNS"
        ),
    )

    # ── Definição do fluxo ────────────────────────────────────────────────
    #
    #   monitorar_s3  ──►  processar_arquivos  ──►  notificar_falha
    #                                                (só se falhar)
    #
    # O operador '>>' define dependência: a tarefa da direita só executa
    # após a da esquerda concluir com sucesso (exceto onde trigger_rule
    # altera esse comportamento, como em notificar_falha).
    monitorar_s3 >> processar >> notificar
