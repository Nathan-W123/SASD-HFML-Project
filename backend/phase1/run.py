import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pyodbc
import pandas as pd
import openpyxl

from config_loader import load_config


def run(log_fn):
    config = load_config()

    conn_str = (
        f"Driver={{SQL Server}};"
        f"Server={config['sql_server']};"
        f"Database={config['sql_database']};"
        f"Trusted_Connection=yes;"
    )

    log_fn("Reading SQL script...")
    with open(config["sql_query_path"], "r") as f:
        query = f.read()

    log_fn("Connecting to SQL Server...")
    conn = pyodbc.connect(conn_str)
    df = pd.read_sql_query(query, conn)
    conn.close()
    log_fn(f"{len(df)} rows returned from query")

    for col in df.select_dtypes(include="object").columns:
        converted = pd.to_numeric(df[col], errors="coerce")
        if converted.notna().all():
            df[col] = converted

    log_fn("Writing to Excel import sheet...")
    wb = openpyxl.load_workbook(config["excel_workbook_path"])

    if "import" in wb.sheetnames:
        del wb["import"]

    # Write the DataFrame's values directly: a CSV round trip turns every number
    # and date (including the conversions above) back into text.
    ws = wb.create_sheet("import")
    ws.append(list(df.columns))
    for row in df.astype(object).itertuples(index=False):
        ws.append([None if pd.isna(value) else value for value in row])

    wb.save(config["excel_workbook_path"])

    log_fn(f"Phase 1 complete — {len(df)} rows imported")
