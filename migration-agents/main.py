"""
Migration Squad — Entry Point.

Bootstraps the CrewAI Crew that orchestrates both migration and QA agents,
then executes the full pipeline against the provided source artifacts.

Usage:
    python main.py \
        --sql   path/to/query.sql \
        --fn    path/to/cloud_function.py \
        --dag   path/to/legacy_dag.py \
        --src-table  my_project.dataset.my_table \
        --tgt-table  MY_DB.PUBLIC.MY_TABLE

Environment variables (required):
    AWS_DEFAULT_REGION      — e.g. us-east-1
    BEDROCK_MODEL_ID        — e.g. anthropic.claude-3-5-sonnet-20241022-v2:0
    AWS_ACCESS_KEY_ID       — AWS credentials (or use an IAM role / instance profile)
    AWS_SECRET_ACCESS_KEY
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from crewai import Crew, Process
from dotenv import load_dotenv
from langchain_aws import ChatBedrock

from agents import (
    build_backend_refactor_agent,
    build_dag_orchestrator_agent,
    build_data_parity_agent,
    build_secops_agent,
    build_sql_translator_agent,
    build_unit_test_agent,
)
from tasks import (
    build_backend_refactor_task,
    build_dag_conversion_task,
    build_data_parity_task,
    build_secops_task,
    build_sql_translation_task,
    build_unit_test_task,
)

load_dotenv()


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------

def _build_llm() -> ChatBedrock:
    """Instantiate the Bedrock LLM used by all agents."""
    model_id = os.environ.get(
        "BEDROCK_MODEL_ID",
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
    )
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    return ChatBedrock(
        model_id=model_id,
        region_name=region,
        model_kwargs={
            "max_tokens": 8192,
            "temperature": 0.1,   # low temp for deterministic code generation
        },
    )


# ---------------------------------------------------------------------------
# Crew factory
# ---------------------------------------------------------------------------

def build_migration_crew(
    sql_path: str,
    function_path: str,
    dag_path: str,
    source_table: str,
    target_table: str,
    schedule_interval: str = "@daily",
    coverage_threshold: int = 85,
    tolerance_pct: float = 0.0,
) -> Crew:
    """
    Assemble and return the full Migration + QA Crew.

    The crew runs sequentially: Migration Squad first, then QA Squad.
    QA tasks receive Migration outputs as context so they can validate the
    generated artifacts without repeating file reads.

    Args:
        sql_path:           Path to the BigQuery SQL script.
        function_path:      Path to the Cloud Function source file.
        dag_path:           Path to the legacy DAG / scheduling script.
        source_table:       Fully-qualified BigQuery table (project.dataset.table).
        target_table:       Fully-qualified Snowflake table (db.schema.table).
        schedule_interval:  Airflow schedule string for the converted DAG.
        coverage_threshold: Minimum line coverage % expected by the test agent.
        tolerance_pct:      Maximum allowed parity deviation (0 = exact match).

    Returns:
        A configured CrewAI Crew ready to be kicked off.
    """
    llm = _build_llm()

    # -- Agents ----------------------------------------------------------
    sql_agent   = build_sql_translator_agent(llm)
    fn_agent    = build_backend_refactor_agent(llm)
    dag_agent   = build_dag_orchestrator_agent(llm)
    test_agent  = build_unit_test_agent(llm)
    sec_agent   = build_secops_agent(llm)
    parity_agent = build_data_parity_agent(llm)

    # -- Migration Tasks -------------------------------------------------
    sql_task = build_sql_translation_task(sql_agent, sql_path)
    fn_task  = build_backend_refactor_task(fn_agent, function_path)
    dag_task = build_dag_conversion_task(dag_agent, dag_path, schedule_interval)

    # -- QA Tasks (depend on migration outputs) --------------------------
    test_task   = build_unit_test_task(test_agent, [fn_task], coverage_threshold)
    sec_task    = build_secops_task(sec_agent, [sql_task, fn_task, dag_task])
    parity_task = build_data_parity_task(
        parity_agent, sql_task, source_table, target_table, tolerance_pct
    )

    return Crew(
        agents=[sql_agent, fn_agent, dag_agent, test_agent, sec_agent, parity_agent],
        tasks=[sql_task, fn_task, dag_task, test_task, sec_task, parity_task],
        process=Process.sequential,
        verbose=True,
        memory=False,   # set True to enable cross-task memory via embeddings
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GCP → AWS Migration Squad powered by CrewAI + AWS Bedrock.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--sql",       required=True,  help="Path to BigQuery SQL script.")
    parser.add_argument("--fn",        required=True,  help="Path to Cloud Function source.")
    parser.add_argument("--dag",       required=True,  help="Path to legacy DAG/script.")
    parser.add_argument("--src-table", required=True,  help="BigQuery table (project.dataset.table).")
    parser.add_argument("--tgt-table", required=True,  help="Snowflake table (db.schema.table).")
    parser.add_argument("--schedule",  default="@daily", help="Airflow schedule interval.")
    parser.add_argument("--coverage",  type=int, default=85, help="Min test coverage %%.")
    parser.add_argument("--tolerance", type=float, default=0.0, help="Parity tolerance %%.")
    return parser.parse_args()


def _validate_paths(*paths: str) -> None:
    for p in paths:
        if not Path(p).exists():
            print(f"ERROR: File not found — '{p}'", file=sys.stderr)
            sys.exit(1)


def main() -> None:
    args = _parse_args()
    _validate_paths(args.sql, args.fn, args.dag)

    print("\n" + "=" * 70)
    print("  MIGRATION SQUAD — GCP → AWS")
    print("=" * 70)
    print(f"  SQL script   : {args.sql}")
    print(f"  Cloud Fn     : {args.fn}")
    print(f"  Legacy DAG   : {args.dag}")
    print(f"  Source table : {args.src_table}")
    print(f"  Target table : {args.tgt_table}")
    print(f"  Schedule     : {args.schedule}")
    print(f"  Coverage     : {args.coverage}%")
    print(f"  Tolerance    : {args.tolerance}%")
    print("=" * 70 + "\n")

    crew = build_migration_crew(
        sql_path=args.sql,
        function_path=args.fn,
        dag_path=args.dag,
        source_table=args.src_table,
        target_table=args.tgt_table,
        schedule_interval=args.schedule,
        coverage_threshold=args.coverage,
        tolerance_pct=args.tolerance,
    )

    result = crew.kickoff()

    print("\n" + "=" * 70)
    print("  CREW RESULT")
    print("=" * 70)
    print(result)


if __name__ == "__main__":
    main()
