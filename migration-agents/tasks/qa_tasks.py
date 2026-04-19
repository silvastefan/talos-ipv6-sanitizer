"""
Tasks for the QA & Validation Squad.

These tasks receive the Migration Squad outputs as context and verify
correctness, security, and data parity.
"""

from crewai import Agent, Task


def build_unit_test_task(
    agent: Agent,
    migration_tasks: list[Task],
    coverage_threshold: int = 85,
) -> Task:
    """
    Task 4 — Generate a pytest suite for the refactored backend code.

    Args:
        agent: The Unit Test Generator agent.
        migration_tasks: Completed migration tasks whose output is the code under test.
        coverage_threshold: Minimum theoretical line-coverage percentage required.

    Returns:
        A CrewAI Task ready to be added to a Crew.
    """
    return Task(
        description=(
            "Analyse the refactored AWS Lambda / ECS code produced by the Backend "
            "Refactorer agent (available in context). Generate a comprehensive pytest "
            "test suite. "
            "Requirements:\n"
            f"  • Achieve ≥ {coverage_threshold} % theoretical line coverage.\n"
            "  • Use `moto` to mock S3, SQS, SNS, and Secrets Manager; use "
            "`pytest-mock` for all other external calls.\n"
            "  • Each function under test must have: one happy-path test, one test "
            "for invalid input, and one test for a downstream service failure.\n"
            "  • Use `pytest.fixture` for shared setup (boto3 clients, env vars).\n"
            "  • All test functions must have a one-line docstring explaining what "
            "they assert.\n"
            "  • Tests must be runnable with `pytest` from the project root without "
            "additional configuration."
        ),
        expected_output=(
            "A Python test file (`test_<module_name>.py`) with:\n"
            "1. All required imports (pytest, moto, pytest-mock).\n"
            "2. Fixtures section.\n"
            "3. Test functions grouped by the function they test.\n"
            "4. A trailing comment listing untested code paths (if any)."
        ),
        agent=agent,
        context=migration_tasks,
    )


def build_secops_task(
    agent: Agent,
    migration_tasks: list[Task],
) -> Task:
    """
    Task 5 — Static security analysis and IAM compliance review.

    Args:
        agent: The SecOps Analyst agent.
        migration_tasks: Completed migration tasks whose output will be analysed.

    Returns:
        A CrewAI Task ready to be added to a Crew.
    """
    return Task(
        description=(
            "Perform a static security analysis on all code artifacts produced by the "
            "Migration Squad (available in context). "
            "Requirements:\n"
            "  • Scan for hardcoded secrets: API keys, passwords, tokens, connection "
            "strings, private keys.\n"
            "  • Identify use of insecure functions: `eval()`, `exec()`, `shell=True`, "
            "unsafe deserialization.\n"
            "  • Review all IAM policy documents or role definitions for wildcard actions "
            "(`*`) and wildcard resources (`*`) where specific ARNs should be used.\n"
            "  • Check that all credentials are sourced from environment variables or "
            "AWS Secrets Manager — never from code.\n"
            "  • Verify S3 bucket policies do not allow public access.\n"
            "  • Produce a structured report with one entry per finding."
        ),
        expected_output=(
            "A Markdown security report with:\n"
            "1. Executive Summary (total findings by severity).\n"
            "2. Findings table: | Severity | File | Line | Description | Remediation |\n"
            "3. IAM Least-Privilege Assessment section.\n"
            "4. PASSED / FAILED overall verdict."
        ),
        agent=agent,
        context=migration_tasks,
    )


def build_data_parity_task(
    agent: Agent,
    sql_translation_task: Task,
    source_table: str,
    target_table: str,
    tolerance_pct: float = 0.0,
) -> Task:
    """
    Task 6 — Generate cross-platform SQL scripts to validate data parity.

    Args:
        agent: The Data Parity Validator agent.
        sql_translation_task: The completed SQL translation task (provides schema context).
        source_table: Fully-qualified BigQuery table name (project.dataset.table).
        target_table: Fully-qualified Snowflake table name (database.schema.table).
        tolerance_pct: Allowed percentage deviation (0.0 = exact match required).

    Returns:
        A CrewAI Task ready to be added to a Crew.
    """
    return Task(
        description=(
            f"Using the schema information from the SQL translation task (available in "
            f"context), generate two SQL validation scripts — one for BigQuery and one "
            f"for Snowflake — that compare '{source_table}' (source) against "
            f"'{target_table}' (target). Allowed tolerance: {tolerance_pct} %.\n"
            "Each script must compute:\n"
            "  • Total row count.\n"
            "  • SUM, AVG, MIN, MAX for every numeric column.\n"
            "  • NULL count per column.\n"
            "  • DISTINCT value count for every categorical (string/boolean) column.\n"
            "Additionally produce a comparison query (Snowflake dialect) that loads the "
            "BigQuery results (assumed to be exported to S3 as CSV) and computes the "
            "delta for each metric, flagging any deviation beyond the tolerance threshold."
        ),
        expected_output=(
            "Three SQL files (plain text, clearly delimited):\n"
            "1. `parity_check_bigquery.sql` — BigQuery-dialect profiling query.\n"
            "2. `parity_check_snowflake.sql` — Snowflake-dialect profiling query.\n"
            "3. `parity_comparison.sql` — Snowflake query that loads BQ results from S3 "
            "and computes deltas, with a final PASS/FAIL verdict per metric."
        ),
        agent=agent,
        context=[sql_translation_task],
    )
