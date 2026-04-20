import pandas as pd
import pyodbc
import openpyxl
import os

# --- CONFIGURATION ---
SERVER_NAME = 'SDP-MaxDayOff'
DATABASE_NAME = 'MAXIMO'

SQL_FILE_PATH = r"\\sdfiles\Shared\SASD_Ops\Engineering\Bus_Planning\4. Regulatory Support\Requests within SacSewer (Internal)\ENG\Business Planning\Stop_The_Clog\High Frequency PMs\Queries\Hydro_PMs.sql"
EXISTING_EXCEL_PATH = r"\\sdfiles\Shared\SASD_Ops\Engineering\Bus_Planning\4. Regulatory Support\Requests within SacSewer (Internal)\ENG\Business Planning\Stop_The_Clog\High Frequency PMs\Hydro_1_3_6_07_2025.xlsx"


def run_sql_and_add_formulas():
    conn_str = (
        f"Driver={{SQL Server}};"
        f"Server={SERVER_NAME};"
        f"Database={DATABASE_NAME};"
        f"Trusted_Connection=yes;"
    )

    try:
        # 1. READ SQL & PULL DATA
        print(f"Reading SQL script from:\n{SQL_FILE_PATH}")
        with open(SQL_FILE_PATH, 'r') as file:
            query = file.read()

        print("\nConnecting to SQL Server and pulling data...")
        conn = pyodbc.connect(conn_str)
        df = pd.read_sql_query(query, conn)
        conn.close()

        # --- DATA CLEANING STEP ---
        print("Converting text IDs to numeric values...")
        df = df.apply(lambda col: pd.to_numeric(col, errors='ignore'))

        # 2. APPEND 'new data' TAB
        print("Writing to the 'new data' tab...")
        with pd.ExcelWriter(EXISTING_EXCEL_PATH, engine='openpyxl', mode='a', if_sheet_exists='replace') as writer:
            df.to_excel(writer, sheet_name='new data', index=False)

        # 3. ADD FORMULAS TO BOTH TABS
        print("\nOpening workbook to add formulas...")
        wb = openpyxl.load_workbook(EXISTING_EXCEL_PATH)

        # --- Update the 'mainlines' sheet ---
        if 'mainlines' in wb.sheetnames:
            ws_main = wb['mainlines']
            max_row_main = ws_main.max_row

            ws_main['V1'] = 'Hydro Result'
            ws_main['W1'] = 'Frequency Check'

            for row in range(2, max_row_main + 1):
                ws_main[f'V{row}'] = f"=VLOOKUP(A{row}, 'new data'!A:G, 7, 0)"
                ws_main[f'W{row}'] = f'=IF(V{row}=G{row}, "", "Change Frequency")'

            print(f"Updated 'mainlines' formulas down to row {max_row_main}.")
        else:
            print("\nWARNING: The sheet 'mainlines' was not found.")

        # --- Update the 'new data' sheet ---
        if 'new data' in wb.sheetnames:
            ws_new = wb['new data']
            max_row_new = ws_new.max_row

            # Adding header to Column N (14th column, right after the 13 SQL columns)
            ws_new['N1'] = 'Mainlines Lookup'

            for row in range(2, max_row_new + 1):
                # VLOOKUP pulling from mainlines into new data
                ws_new[f'N{row}'] = f"=VLOOKUP(A{row}, 'mainlines'!A:G, 7, 0)"

            print(f"Updated 'new data' formulas down to row {max_row_new}.")

        # Force Excel to recalculate formulas upon opening
        # We will make 'mainlines' the active tab that opens first
        if 'mainlines' in wb.sheetnames:
            wb.active = wb['mainlines']

            # Save the final workbook
        wb.save(EXISTING_EXCEL_PATH)
        print("\nSuccess! Workbook saved and closed.")

    except PermissionError:
        print("\nERROR: Permission Denied. Please make sure the Excel file is CLOSED before running the script.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")


if __name__ == "__main__":
    run_sql_and_add_formulas()