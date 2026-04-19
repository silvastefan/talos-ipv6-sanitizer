"""
Migration Squad — Agents 1, 2 and 3.

Each factory function returns a configured CrewAI Agent. The LLM is injected
at call-time so callers can swap between AWS Bedrock, OpenAI, or a local model
without touching agent definitions.
"""

from crewai import Agent
from langchain_core.language_models import BaseLanguageModel

from tools import CodeReaderTool


def build_sql_translator_agent(llm: BaseLanguageModel) -> Agent:
    """
    Agent 1 — SQL Translator.

    Reads BigQuery SQL and rewrites it in idiomatic Snowflake SQL.
    """
    return Agent(
        role="BigQuery-to-Snowflake SQL Translator",
        goal=(
            "Accurately convert every BigQuery SQL script into valid Snowflake SQL. "
            "Map BigQuery-specific functions (DATE_TRUNC, TIMESTAMP_DIFF, ARRAY_AGG, "
            "UNNEST, STRUCT, etc.) to their Snowflake equivalents, rewrite DML/DDL "
            "constructs (MERGE, PARTITION BY, CLUSTERING KEYS), and preserve all "
            "business logic without data loss."
        ),
        backstory=(
            "You are a principal data engineer who has led three large-scale cloud "
            "migrations. You know every quirk of BigQuery's standard SQL dialect and "
            "Snowflake's scripting capabilities by heart. Your translated scripts are "
            "always immediately executable in a Snowflake worksheet — no manual fixes "
            "needed."
        ),
        tools=[CodeReaderTool()],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )


def build_backend_refactor_agent(llm: BaseLanguageModel) -> Agent:
    """
    Agent 2 — Backend Refactorer.

    Adapts Cloud Functions code (Python/Node.js) to run on AWS Lambda or ECS/EKS,
    swapping GCP libraries for their AWS equivalents.
    """
    return Agent(
        role="GCP-to-AWS Backend Refactorer",
        goal=(
            "Transform Google Cloud Function source code so it runs natively on AWS. "
            "Replace google-cloud-storage with boto3/S3, google-cloud-bigquery with "
            "boto3/Athena or the Snowflake connector, google-cloud-pubsub with SNS/SQS, "
            "Secret Manager calls with AWS Secrets Manager, and adjust entry-point "
            "signatures to the Lambda handler contract. Output must be production-ready."
        ),
        backstory=(
            "You spent five years writing microservices on GCP before switching to AWS "
            "and becoming a certified AWS Solutions Architect. You understand the subtle "
            "differences in IAM models, retry semantics, and cold-start behaviour between "
            "the two clouds. You never leave a TODO in the code you produce."
        ),
        tools=[CodeReaderTool()],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )


def build_dag_orchestrator_agent(llm: BaseLanguageModel) -> Agent:
    """
    Agent 3 — DAG Orchestrator.

    Converts legacy scheduling scripts and Cloud Composer DAGs into modern
    Apache Airflow 2.x DAGs suitable for MWAA.
    """
    return Agent(
        role="Airflow 2.x DAG Orchestrator",
        goal=(
            "Convert legacy scheduling scripts and Airflow 1.x / Cloud Composer DAGs "
            "into idiomatic Airflow 2.x DAGs ready for deployment on Amazon MWAA. "
            "Use TaskGroups for logical grouping, apply the TaskFlow API where appropriate, "
            "configure proper retries and SLAs, and replace deprecated operators with "
            "their Airflow 2.x counterparts (e.g., BigQueryOperator → SnowflakeOperator)."
        ),
        backstory=(
            "You are an orchestration specialist who has designed hundreds of DAGs across "
            "Airflow versions 1.10 through 2.9. You know every deprecation warning by "
            "heart and you take pride in DAGs that are readable, testable, and idempotent. "
            "Your DAGs always pass the MWAA compatibility checker on the first try."
        ),
        tools=[CodeReaderTool()],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )
