"""Minimal CSV exporter for synthetic data tables.

Implements data_dictionary.md section 0 and Phase 1 export requirements:
- Writes each DataFrame to data/raw/<table>.csv
- UTF-8 encoding
- "\\n" (LF) line endings
- Empty string for nulls
- true/false lowercase booleans
- Fixed decimals for float columns
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping
import pandas as pd


def table_to_filename(table_name: str) -> str:
    """Convert table name (e.g. 'AgentShift') to CSV filename (e.g. 'agent_shift.csv')."""
    clean = table_name.removesuffix(".csv")
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", clean)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()
    return f"{snake}.csv"


def format_dataframe_for_export(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare DataFrame for CSV serialization with literal compliance to data_dictionary.md 0."""
    out = df.copy()

    for col in out.columns:
        # 1. Format boolean columns as literal 'true' / 'false'
        if out[col].dtype == "bool":
            out[col] = out[col].map({True: "true", False: "false"})
        elif out[col].dtype == "object":
            # Handle object columns that may contain python bools
            out[col] = out[col].map(
                lambda x: "true" if x is True else ("false" if x is False else x)
            )

        # 2. Fixed decimals for float columns
        if pd.api.types.is_float_dtype(out[col]):
            # If column is volume_weight_pct or similar, format with 1 decimal place
            out[col] = out[col].map(
                lambda x: "" if pd.isna(x) else f"{x:.1f}"
            )

    return out


def export_tables(
    tables: Mapping[str, pd.DataFrame], out_dir: str | Path
) -> dict[str, Path]:
    """Export each table DataFrame to a CSV file.

    Args:
        tables: Mapping from table name to pandas DataFrame.
        out_dir: Destination directory path.

    Returns:
        Mapping from table name to destination file Path.
    """
    dest_dir = Path(out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    exported: dict[str, Path] = {}
    for table_name in sorted(tables.keys()):
        df = tables[table_name]
        filename = table_to_filename(table_name)
        file_path = dest_dir / filename

        formatted_df = format_dataframe_for_export(df)
        formatted_df.to_csv(
            file_path,
            index=False,
            encoding="utf-8",
            lineterminator="\n",
            na_rep="",
        )
        exported[table_name] = file_path

    return exported
