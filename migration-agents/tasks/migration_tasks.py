"""
Tasks for the Migration Squad.

Each factory receives the corresponding Agent and the runtime context
(source file paths, table names, etc.) and returns a configured Task.
The output of each task feeds into the QA Squad tasks as context.
"""

from crewai import Agent, Task


def build_sql_translation_task(
    agent: Agent,
    bigquery_script_path: str,
    target_schema: str = "PUBLIC",
) -> Task:
    """
    Task 1 — Translate a BigQuery SQL script to Snowflake SQL.

    Args:
        agent: The SQL Translator agent.
        bigquery_script_path: Path to the .sql file to translate.
        target_schema: Snowflake schema to use in the translated script.

    Returns:
        A CrewAI Task ready to be added to a Crew.
    """
    return Task(
        description=(
            f"Read the BigQuery SQL script located at '{bigquery_script_path}' using "
            f"the code_reader tool. Translate every statement into valid Snowflake SQL "
            f"targeting the '{target_schema}' schema. "
            "Requirements:\n"
            "  • Replace all BigQuery-specific functions with their Snowflake equivalents "
            "(DATE_TRUNC, TIMESTAMP_DIFF → DATEDIFF, ARRAY_AGG → ARRAY_AGG with proper "
            "syntax, UNNEST → LATERAL FLATTEN, STRUCT → OBJECT_CONSTRUCT, etc.).\n"
            "  • Rewrite PARTITION BY / CLUSTER BY clauses as Snowflake CLUSTER ON.\n"
            "  • Convert any Legacy SQL syntax to Snowflake Scripting if needed.\n"
            "  • Add a header comment block listing every transformation applied.\n"
            "  • Do NOT change any business logic — the output must be semantically "
            "identical to the input."
        ),
        expected_output=(
            "A single Snowflake SQL script (plain text) with:\n"
            "1. A comment block at the top listing all transformations applied.\n"
            "2. The fully translated SQL statements.\n"
            "3. A summary section (SQL comments) noting any assumptions made."
        ),
        agent=agent,
    )


def build_backend_refactor_task(
    agent: Agent,
    source_function_path: str,
    runtime: str = "python3.12",
) -> Task:
    """
    Task 2 — Refactor a Cloud Function to run on AWS Lambda / ECS.

    Args:
        agent: The Backend Refactorer agent.
        source_function_path: Path to the Cloud Function source file.
        runtime: Target AWS Lambda runtime identifier.

    Returns:
        A CrewAI Task ready to be added to a Crew.
    """
    return Task(
        description=(
            f"Read the Cloud Function source code at '{source_function_path}' using the "
            f"code_reader tool. Refactor it to run on AWS Lambda ({runtime}) or ECS/EKS. "
            "Requirements:\n"
            "  • Replace google-cloud-storage with boto3 (s3 client).\n"
            "  • Replace google-cloud-bigquery with the Snowflake Python connector or "
            "boto3 Athena, depending on the operation.\n"
            "  • Replace google-cloud-pubsub with boto3 SNS/SQS.\n"
            "  • Replace Secret Manager calls with boto3 secretsmanager.\n"
            "  • Adapt the entry-point to the Lambda handler signature: "
            "`def handler(event: dict, context: LambdaContext) -> dict`.\n"
            "  • Remove all GCP-specific imports; add only the necessary AWS imports.\n"
            "  • All credentials must be read from environment variables — no hardcoding.\n"
            "  • Include a requirements.txt snippet for the new dependencies."
        ),
        expected_output=(
            "Refactored Python module (plain text) with:\n"
            "1. Updated import section.\n"
            "2. AWS-compatible handler function.\n"
            "3. All helper functions adapted for AWS SDKs.\n"
            "4. A `# requirements.txt additions` comment block at the bottom."
        ),
        agent=agent,
    )


def build_dag_conversion_task(
    agent: Agent,
    source_dag_path: str,
    schedule_interval: str = "@daily",
) -> Task:
    """
    Task 3 — Convert a legacy DAG / scheduling script to Airflow 2.x for MWAA.

    Args:
        agent: The DAG Orchestrator agent.
        source_dag_path: Path to the legacy DAG or scheduling script.
        schedule_interval: Desired Airflow schedule expression.

    Returns:
        A CrewAI Task ready to be added to a Crew.
    """
    return Task(
        description=(
            f"Read the legacy DAG or scheduling script at '{source_dag_path}' using "
            f"the code_reader tool. Produce an Apache Airflow 2.x DAG file compatible "
            f"with Amazon MWAA, scheduled as '{schedule_interval}'. "
            "Requirements:\n"
            "  • Use the `@dag` decorator (TaskFlow API) wherever possible.\n"
            "  • Group logically related tasks inside `TaskGroup` blocks.\n"
            "  • Replace deprecated operators (BigQueryOperator → SnowflakeOperator, "
            "GCSToS3Operator, etc.) with their Airflow 2.x / MWAA-available equivalents.\n"
            "  • Configure `retries=3`, `retry_delay=timedelta(minutes=5)`, and "
            "appropriate `sla` values on each task.\n"
            "  • Ensure DAG is idempotent: re-running a task for the same `execution_date` "
            "must produce the same result.\n"
            "  • Include docstring on the DAG function describing its business purpose."
        ),
        expected_output=(
            "A complete Airflow 2.x Python DAG file (plain text) with:\n"
            "1. Proper imports from apache-airflow 2.x and airflow.providers.\n"
            "2. TaskGroup definitions grouping related operators.\n"
            "3. Clear task dependencies (>> operators).\n"
            "4. Inline comments explaining non-obvious design choices."
        ),
        agent=agent,
    )
