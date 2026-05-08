import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

REQUIRED_KEYS = [
    "sql_server",
    "sql_database",
    "sql_query_path",
    "excel_workbook_path",
    "excel_working_sheet",
    "arcgis_project_path",
    "feature_class_path",
    "template_vertical_path",
    "template_horizontal_path",
    "pdf_output_dir",
    "unique_id_field",
    "p4_ml_layer_path",
    "p4_ml_source_path",
    "p4_ml_dest_path",
    "p4_ll_source_path",
    "p4_ll_dest_path",
    "p4_parcels_source_path",
    "p4_parcels_dest_path",
]

PATH_KEYS = [
    "sql_query_path",
    "excel_workbook_path",
    "arcgis_project_path",
    "feature_class_path",
    "template_vertical_path",
    "template_horizontal_path",
    "pdf_output_dir",
    "p4_ml_layer_path",
    "p4_ml_source_path",
    "p4_ml_dest_path",
    "p4_ll_source_path",
    "p4_ll_dest_path",
    "p4_parcels_source_path",
    "p4_parcels_dest_path",
]


def _resolve_path(path):
    if not isinstance(path, str) or not path:
        return path
    if os.path.isabs(path) or path.startswith("\\\\"):
        return path
    return os.path.abspath(os.path.join(PROJECT_ROOT, path))


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"config.json not found at {CONFIG_PATH}")

    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    missing = [k for k in REQUIRED_KEYS if k not in config]
    if missing:
        raise KeyError(f"config.json is missing required keys: {', '.join(missing)}")

    for key in PATH_KEYS:
        config[key] = _resolve_path(config[key])

    return config
