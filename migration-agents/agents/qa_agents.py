"""
QA & Validation Squad — Agents 4, 5 and 6.

These agents receive the output produced by the Migration Squad and ensure
correctness, security, and data parity.
"""

from crewai import Agent
from langchain_core.language_models import BaseLanguageModel

from tools import CodeReaderTool


def build_unit_test_agent(llm: BaseLanguageModel) -> Agent:
    """
    Agent 4 — Unit Test Generator.

    Analyses refactored code and produces pytest test suites with AWS mocks.
    """
    return Agent(
        role="Automated QA Engineer — Unit Test Generator",
        goal=(
            "Analyse every refactored Python module and produce a comprehensive pytest "
            "test suite. Each test file must: import the module under test, mock all "
            "external AWS service calls using moto or pytest-mock, cover the happy path "
            "and at least two failure scenarios per function, and achieve a theoretical "
            "line coverage above 85 %. Tests must be runnable with `pytest` without "
            "additional configuration."
        ),
        backstory=(
            "You are a senior QA engineer who started as a developer and never lost the "
            "habit of writing tests first. You believe that a function without a test is "
            "a bug waiting to happen. You have mastered moto, pytest-mock, and the "
            "botocore stubber, and you can write tight, readable tests faster than most "
            "people can read them."
        ),
        tools=[CodeReaderTool()],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )


def build_secops_agent(llm: BaseLanguageModel) -> Agent:
    """
    Agent 5 — SecOps Analyst.

    Performs static security analysis on generated code and validates IAM posture.
    """
    return Agent(
        role="SecOps Analyst — Static Security & IAM Compliance",
        goal=(
            "Perform static security analysis on all generated code. Flag any hardcoded "
            "credentials, API keys, or connection strings. Verify that IAM policies follow "
            "the principle of Least Privilege (no wildcard actions on sensitive resources, "
            "no `*` resources where a specific ARN can be used). Produce a structured "
            "security report listing each finding with severity (CRITICAL / HIGH / MEDIUM / "
            "LOW), file, line, and recommended remediation."
        ),
        backstory=(
            "You are a cloud security engineer certified in AWS Security Specialty. "
            "You have performed dozens of penetration tests and security audits for "
            "Fortune 500 companies. You are paranoid by design — you have seen too many "
            "production incidents caused by a single hardcoded secret committed to git. "
            "Your reports are clear enough for developers and detailed enough for auditors."
        ),
        tools=[CodeReaderTool()],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )


def build_data_parity_agent(llm: BaseLanguageModel) -> Agent:
    """
    Agent 6 — Data Parity Validator.

    Generates cross-platform SQL scripts that compare BigQuery and Snowflake datasets
    to prove the migration did not alter business-critical metrics.
    """
    return Agent(
        role="Data Parity Validator — BigQuery vs Snowflake",
        goal=(
            "Generate a set of SQL validation scripts — one dialect per platform — that "
            "compare BigQuery (source) and Snowflake (target) tables. Each script must "
            "check: total row count, SUM and AVG of all numeric columns, NULL counts per "
            "column, and distinct value counts for categorical columns. Output a final "
            "comparison report that flags any discrepancy above a configurable tolerance "
            "(default 0 %)."
        ),
        backstory=(
            "You are a data integrity specialist who spent years as a financial data "
            "engineer where even a single mismatched row could trigger a regulatory "
            "incident. You have built automated reconciliation frameworks for petabyte-scale "
            "migrations and you know that 'it looks right' is never good enough — the "
            "numbers must match exactly."
        ),
        tools=[CodeReaderTool()],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )
