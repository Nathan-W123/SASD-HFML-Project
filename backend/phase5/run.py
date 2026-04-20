import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json
import shutil
import arcpy
import arcpy.mp

from config_loader import load_config

# TODO: verify these match the layer name and definition query field in the .aprx templates
HFML_LAYER_NAME    = "TODO: verify HFML layer name in template .aprx"
HFML_ID_FIELD      = "TODO: verify HFML ID field name used in definition query"

# TODO: verify dynamic text element names in the .aprx templates
TEXT_ELEMENT_TITLE = "TODO: verify title text element name"
TEXT_ELEMENT_DATE  = "TODO: verify date text element name"
TEXT_ELEMENT_ID    = "TODO: verify HFML ID text element name"

MAP_BUFFER = 1.2  # extent zoom buffer multiplier

TEMP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "temp", "added_mls.json")


def _select_template(extent, config):
    width  = extent.XMax - extent.XMin
    height = extent.YMax - extent.YMin
    if width >= height:
        return config["template_horizontal_path"]
    return config["template_vertical_path"]


def _get_hfml_extent(hfml_id, feature_class):
    with arcpy.da.SearchCursor(feature_class, ["SHAPE@"], f"{HFML_ID_FIELD} = '{hfml_id}'") as cursor:
        extents = [row[0].extent for row in cursor]
    if not extents:
        raise ValueError(f"No features found for HFML {hfml_id}")
    xmin = min(e.XMin for e in extents)
    ymin = min(e.YMin for e in extents)
    xmax = max(e.XMax for e in extents)
    ymax = max(e.YMax for e in extents)
    return arcpy.Extent(xmin, ymin, xmax, ymax)


def run(log_fn):
    config = load_config()

    if not os.path.exists(TEMP_PATH):
        raise FileNotFoundError("added_mls.json not found — run Phase 2 first")

    with open(TEMP_PATH, "r") as f:
        added_ml_ids = json.load(f)

    if not added_ml_ids:
        log_fn("No new HFMLs to process — Phase 5 complete")
        return

    from datetime import date
    today = date.today().strftime("%Y-%m-%d")

    exported = 0
    for hfml_id in added_ml_ids:
        log_fn(f"Generating map for HFML {hfml_id}...")

        extent   = _get_hfml_extent(hfml_id, config["feature_class_path"])
        template = _select_template(extent, config)

        tmp_aprx_path = os.path.join(config["pdf_output_dir"], f"_tmp_{hfml_id}.aprx")
        shutil.copy(template, tmp_aprx_path)

        aprx   = arcpy.mp.ArcGISProject(tmp_aprx_path)
        layout = aprx.listLayouts()[0]  # TODO: verify layout name if multiple exist

        # Scope the HFML layer to current ID
        for lyr in aprx.listMaps()[0].listLayers():
            if lyr.name == HFML_LAYER_NAME:
                lyr.definitionQuery = f"{HFML_ID_FIELD} = '{hfml_id}'"

        # Zoom map frame to HFML extent with buffer
        mf = layout.listElements("MAPFRAME_ELEMENT")[0]  # TODO: verify map frame element name
        w = (extent.XMax - extent.XMin) * (MAP_BUFFER - 1) / 2
        h = (extent.YMax - extent.YMin) * (MAP_BUFFER - 1) / 2
        mf.camera.setExtent(arcpy.Extent(
            extent.XMin - w, extent.YMin - h,
            extent.XMax + w, extent.YMax + h
        ))

        # Update dynamic text elements
        for el in layout.listElements("TEXT_ELEMENT"):
            if el.name == TEXT_ELEMENT_TITLE:
                el.text = f"HFML {hfml_id}"
            elif el.name == TEXT_ELEMENT_DATE:
                el.text = today
            elif el.name == TEXT_ELEMENT_ID:
                el.text = str(hfml_id)

        pdf_path = os.path.join(config["pdf_output_dir"], f"{hfml_id}.pdf")
        layout.exportToPDF(pdf_path)

        aprx.save()
        del aprx
        os.remove(tmp_aprx_path)

        log_fn(f"Exported {hfml_id}.pdf")
        exported += 1

    log_fn(f"Phase 5 complete — {exported} PDFs exported to {config['pdf_output_dir']}")
