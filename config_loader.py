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


# Optional keys, with the defaults used when config.json does not list them.
# Keeping them optional means an existing config.json -- including the copy on
# the shared network folder -- keeps working untouched.
OPTIONAL_DEFAULTS = {
    # Phase 4 caching.  The cache holds a local copy of the ML source and the
    # directed pipe network built from it, so the network share is read once
    # when it changes rather than once per HFML.
    "p4_cache_enabled": True,
    # Must be an ABSOLUTE path on a local disk.  Empty means "use the system
    # temp folder", which is what you want: the project folder itself lives on
    # the share for shared users, so caching there would cache nothing useful.
    "p4_cache_dir": "",
    # Rebuild the cache at least this often even if the source looks unchanged.
    # 0 disables the age check.
    "p4_cache_max_age_days": 7,
    # Copy the ML source to the local cache geodatabase.
    "p4_cache_local_ml_copy": True,
    # Ask the ML source only for the assetnums being mapped, instead of reading
    # every row and filtering in Python.
    "p4_filter_assetnum_scan": True,
    # Pipe ends this far apart (feet) are treated as the same manhole when the
    # trace network is built.  The trace matches manholes exactly, so without
    # this a pair of ends 0.01 ft apart breaks the chain and every pipe above
    # the gap is missing from the map.  0 restores exact matching.  Raising it
    # also merges manholes that really are that close, so raise it no further
    # than the gap distances the log reports.
    "p4_node_snap_tolerance_feet": 0.1,
    # Report, once per cache build, how close manholes sit without sharing a
    # key -- this is what should pick the tolerance above.  Read-only.
    "p4_report_network_gaps": True,
    # Report unconnected pipe ends just off the edge of each trace, which is
    # what a cut-off branch looks like.  Read-only.
    "p4_report_trace_gaps": True,
}


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

    for key, default in OPTIONAL_DEFAULTS.items():
        config.setdefault(key, default)

    return config
