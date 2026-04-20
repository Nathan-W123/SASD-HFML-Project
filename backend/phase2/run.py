import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json
import openpyxl
from openpyxl.styles import PatternFill

from config_loader import load_config

HIGHLIGHT_FILL = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")

ML_ID_COL = "pmnum"
FREQ_COL  = "frequency"

TEMP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "temp", "added_mls.json")


def run(log_fn):
    config = load_config()

    wb = openpyxl.load_workbook(config["excel_workbook_path"])
    ws_main   = wb[config["excel_working_sheet"]]
    ws_import = wb["import"]

    # Read headers from both sheets
    main_headers   = [cell.value for cell in next(ws_main.iter_rows(min_row=1, max_row=1))]
    import_headers = [cell.value for cell in next(ws_import.iter_rows(min_row=1, max_row=1))]

    if ML_ID_COL not in main_headers:
        raise ValueError(f"Column '{ML_ID_COL}' not found in mainlines sheet. Headers: {main_headers}")
    if ML_ID_COL not in import_headers:
        raise ValueError(f"Column '{ML_ID_COL}' not found in import sheet. Headers: {import_headers}")

    main_id_idx     = main_headers.index(ML_ID_COL)
    import_id_idx   = import_headers.index(ML_ID_COL)
    main_freq_idx   = main_headers.index(FREQ_COL)
    import_freq_idx = import_headers.index(FREQ_COL)

    # Map mainlines column positions to import column positions by name
    col_map = {}
    for main_col_idx, col_name in enumerate(main_headers):
        if col_name in import_headers:
            col_map[main_col_idx] = import_headers.index(col_name)

    # Build lookup of existing MLs: id -> row index (normalize to str to handle int/str mismatch)
    existing = {}
    for i, row in enumerate(ws_main.iter_rows(min_row=2, values_only=True), start=2):
        existing[str(row[main_id_idx])] = i

    # Build lookup of import MLs: id -> full row
    incoming = {}
    for row in ws_import.iter_rows(min_row=2, values_only=True):
        incoming[str(row[import_id_idx])] = row

    # Detect added MLs (in import, not in mainlines)
    added_ids = [ml_id for ml_id in incoming if ml_id not in existing]
    log_fn(f"{len(added_ids)} new MLs detected")

    # Detect removed MLs (in mainlines, not in import)
    removed_ids = [ml_id for ml_id in existing if ml_id not in incoming]
    log_fn(f"{len(removed_ids)} removed MLs detected")

    # Append added MLs — map import columns to mainlines positions by name,
    # leaving manually-maintained columns (Action, Figure #, etc.) blank
    # Track the starting row so we can highlight the new rows afterward
    first_new_row = ws_main.max_row + 1
    for ml_id in added_ids:
        import_row = incoming[ml_id]
        new_row = [None] * len(main_headers)
        for main_idx, import_idx in col_map.items():
            new_row[main_idx] = import_row[import_idx]
        ws_main.append(new_row)

    # Highlight all newly appended rows in yellow
    for row_idx in range(first_new_row, ws_main.max_row + 1):
        for cell in ws_main[row_idx]:
            cell.fill = HIGHLIGHT_FILL

    # Update frequency for existing MLs where it has changed
    freq_changes = 0
    for ml_id, row_idx in existing.items():
        if ml_id in incoming:
            new_freq = incoming[ml_id][import_freq_idx]
            current_freq = ws_main.cell(row=row_idx, column=main_freq_idx + 1).value
            if str(new_freq) != str(current_freq):
                ws_main.cell(row=row_idx, column=main_freq_idx + 1).value = new_freq
                freq_changes += 1

    log_fn(f"{freq_changes} frequency changes updated")

    # Write removed MLs to a separate sheet for review (do not delete from mainlines)
    if "removed_mls" in wb.sheetnames:
        del wb["removed_mls"]
    ws_removed = wb.create_sheet("removed_mls")
    ws_removed.append(main_headers)
    for ml_id in removed_ids:
        row_idx = existing[ml_id]
        row_data = [ws_main.cell(row=row_idx, column=col + 1).value for col in range(len(main_headers))]
        ws_removed.append(row_data)
    log_fn(f"{len(removed_ids)} removed MLs written to 'removed_mls' sheet")

    del wb["import"]

    wb.save(config["excel_workbook_path"])

    # Write added MLs to temp file for Phase 4 and Phase 5
    os.makedirs(os.path.dirname(TEMP_PATH), exist_ok=True)
    with open(TEMP_PATH, "w") as f:
        json.dump(added_ids, f)

    log_fn(f"Phase 2 complete — {len(added_ids)} MLs added, {freq_changes} frequencies updated, {len(removed_ids)} flagged for removal")
