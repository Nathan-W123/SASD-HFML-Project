import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json

from config_loader import load_config

# Source names match SQL/Excel column names.
# Destination names must match the ArcGIS feature class field names.
FIELD_MAPPINGS = [
    ("pmnum",        "pmnum"),
    ("description",  "description"),
    ("assetnum",     "assetnum"),
    ("gridno",       "gridno"),
    ("lastcompdate", "lastcompdate"),
    ("laststartdate","laststartdate"),
    ("frequency",    "frequency"),
    ("frequnit",     "frequnit"),
    ("worktype",     "worktype"),
    ("jpnum",        "jpnum"),
    ("next_date",    "next_date"),
    ("crewid",       "crewid"),
    ("fcprojectid",  "fcprojectid"),
]

TEMP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "temp", "added_mls.json")
NEW_FLAG_FIELD = "new_flag"


def _import_arcpy():
    try:
        import arcpy
        return arcpy
    except Exception as exc:
        raise RuntimeError(
            "ArcGIS Pro Python is not loading correctly. Launch the app with "
            "launch.bat or run it from ArcGIS Pro's Python environment. "
            f"Current interpreter: {sys.executable}. Original import error: {exc}"
        ) from exc


def run(log_fn):
    log_fn(f"Phase 3 interpreter: {sys.executable}")
    arcpy = _import_arcpy()

    config = load_config()

    product_info = arcpy.ProductInfo()
    if product_info in {"NotInitialized", "Unavailable", "Engine", "ArcServer"}:
        raise RuntimeError(f"ArcGIS Pro license not available: {product_info}")
    log_fn(f"ArcGIS product license: {product_info}")

    arcpy.env.workspace = os.path.dirname(config["feature_class_path"])
    target = config["feature_class_path"]

    # Add new_flag field if it doesn't exist
    existing_fields = [f.name for f in arcpy.ListFields(target)]
    if NEW_FLAG_FIELD not in existing_fields:
        arcpy.management.AddField(target, NEW_FLAG_FIELD, "SHORT")
        log_fn(f"Added '{NEW_FLAG_FIELD}' field to feature class")

    log_fn("Converting Excel to GIS table...")
    scratch_table = "in_memory\\excel_import"
    arcpy.conversion.ExcelToTable(
        config["excel_workbook_path"],
        scratch_table,
        config["excel_working_sheet"]
    )
    log_fn("Excel converted to GIS table")

    log_fn("Appending to attribute table with upsert...")
    field_map = arcpy.FieldMappings()
    for src_field, dst_field in FIELD_MAPPINGS:
        fm = arcpy.FieldMap()
        fm.addInputField(scratch_table, src_field)
        fm.outputField.name = dst_field
        field_map.addFieldMap(fm)

    arcpy.management.Append(
        inputs=scratch_table,
        target=target,
        schema_type="NO_TEST",
        field_mapping=field_map,
        match_fields=[[config["unique_id_field"], config["unique_id_field"]]],
        update_geometry="NOT_UPDATE_GEOMETRY"
    )

    arcpy.management.Delete(scratch_table)
    log_fn("Attribute table updated")

    # Reset all rows to 0, then flag newly added rows as 1
    arcpy.management.CalculateField(target, NEW_FLAG_FIELD, "0", "PYTHON3")

    added_ids = []
    if os.path.exists(TEMP_PATH):
        with open(TEMP_PATH) as f:
            added_ids = [str(x) for x in json.load(f)]

    if added_ids:
        added_id_set = set(added_ids)
        with arcpy.da.UpdateCursor(target, ["pmnum", NEW_FLAG_FIELD]) as cursor:
            for row in cursor:
                if str(row[0]) in added_id_set:
                    row[1] = 1
                    cursor.updateRow(row)
        log_fn(f"{len(added_ids)} new rows flagged in '{NEW_FLAG_FIELD}' field")

    log_fn("Phase 3 complete — attribute table updated")
