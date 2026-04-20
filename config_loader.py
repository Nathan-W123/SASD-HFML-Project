import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

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
]

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"config.json not found at {CONFIG_PATH}")

    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    missing = [k for k in REQUIRED_KEYS if k not in config]
    if missing:
        raise KeyError(f"config.json is missing required keys: {', '.join(missing)}")

    return config
