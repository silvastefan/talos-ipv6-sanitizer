"""Tool for reading source code files from the local filesystem."""

from pathlib import Path
from typing import Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class CodeReaderInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the source file to read.")
    encoding: str = Field(default="utf-8", description="File encoding (default: utf-8).")


class CodeReaderTool(BaseTool):
    """Reads the content of a local source code file and returns it as a string."""

    name: str = "code_reader"
    description: str = (
        "Reads a source code file from the local filesystem and returns its full content. "
        "Use this to inspect BigQuery SQL scripts, Cloud Function code, or any other artifact "
        "that needs to be migrated or analyzed."
    )
    args_schema: Type[BaseModel] = CodeReaderInput

    def _run(self, file_path: str, encoding: str = "utf-8") -> str:
        path = Path(file_path)
        if not path.exists():
            return f"ERROR: File not found — '{file_path}'"
        if not path.is_file():
            return f"ERROR: Path is not a file — '{file_path}'"
        return path.read_text(encoding=encoding)

    # ------------------------------------------------------------------
    # Directory listing helper (bonus utility, not required by BaseTool)
    # ------------------------------------------------------------------
    @staticmethod
    def list_files(directory: str, pattern: str = "**/*") -> list[str]:
        """Return all file paths under *directory* matching *pattern*."""
        return [str(p) for p in Path(directory).glob(pattern) if p.is_file()]
