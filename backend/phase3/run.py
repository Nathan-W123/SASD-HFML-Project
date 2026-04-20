import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import arcpy

from config_loader import load_config

# TODO: verify these field names match the ArcGIS feature class schema
# TODO: add any additional field mappings needed beyond WONUM and FREQUENCY
FIELD_MAPPINGS = [
    ("WONUM",     "WONUM"),
    ("FREQUENCY", "FREQUENCY"),
]


def run(log_fn):
    config = load_config()

    if arcpy.CheckExtension("Standard") != "Available":
        raise RuntimeError("ArcGIS Standard license not available")

    arcpy.env.workspace = config["arcgis_project_path"]

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
        target=config["feature_class_path"],
        schema_type="NO_TEST",
        field_mapping=field_map,
        upsert=True,
        upsert_matching_field=config["unique_id_field"]
    )

    arcpy.management.Delete(scratch_table)

    log_fn("Phase 3 complete — attribute table updated")
