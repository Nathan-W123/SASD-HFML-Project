import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json
import shutil
from datetime import date

from config_loader import load_config
from backend.phase4.run import PHASE4_MAP_OUTPUT_PATH

HFML_LAYER_NAME = "High Freq ML PMs"
HFML_ID_FIELD = "PM_Mainlines_pmnum"
MAP_NAME = "High Freq ML Data Map"
MAP_FRAME_NAME = "Map Frame"
VERTICAL_LAYOUT_NAME = "Vertical Layout"
HORIZONTAL_LAYOUT_NAME = "Horizontal Layout"
DATE_ELEMENT_NAME = "date"
FIGURE_ELEMENT_NAME = "Figure"

MAP_BUFFER = 1.2
MIN_MARGIN_INCHES = 0.5

def _import_arcpy():
    try:
        import arcpy
        import arcpy.mp
        return arcpy
    except Exception as exc:
        raise RuntimeError(
            "ArcGIS Pro Python is not loading correctly. Launch the app with "
            "launch.bat or run it from ArcGIS Pro's Python environment. "
            f"Current interpreter: {sys.executable}. Original import error: {exc}"
        ) from exc


def _sql_literal(value):
    return str(value).replace("'", "''")


def _pm_query(pmnum):
    return f"{HFML_ID_FIELD} = '{_sql_literal(pmnum)}'"


def _query(field_name, value):
    return f"{field_name} = '{_sql_literal(value)}'"


def _resolve_field(arcpy, dataset, candidates):
    lowered = {field.name.lower(): field.name for field in arcpy.ListFields(dataset)}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    raise RuntimeError(
        f"Could not find any of {', '.join(candidates)} in {dataset}. "
        f"Available fields: {sorted(lowered.values())}"
    )


def _extent_union(arcpy, extents):
    xmin = min(ext.XMin for ext in extents)
    ymin = min(ext.YMin for ext in extents)
    xmax = max(ext.XMax for ext in extents)
    ymax = max(ext.YMax for ext in extents)
    return arcpy.Extent(xmin, ymin, xmax, ymax)


def _buffer_extent(arcpy, extent, multiplier=MAP_BUFFER):
    width = max(extent.XMax - extent.XMin, 1)
    height = max(extent.YMax - extent.YMin, 1)
    pad_x = width * (multiplier - 1) / 2
    pad_y = height * (multiplier - 1) / 2
    return arcpy.Extent(
        extent.XMin - pad_x,
        extent.YMin - pad_y,
        extent.XMax + pad_x,
        extent.YMax + pad_y,
    )


def _apply_camera_extent(arcpy, map_frame, raw_extent):
    buffered = _buffer_extent(arcpy, raw_extent)

    frame_w = map_frame.elementWidth
    frame_h = map_frame.elementHeight
    buf_w = max(buffered.XMax - buffered.XMin, 1)
    buf_h = max(buffered.YMax - buffered.YMin, 1)

    # ArcGIS fits the extent into the frame preserving aspect ratio; the
    # axis that needs more zoom-out sets the effective map scale.
    scale = max(buf_w / frame_w, buf_h / frame_h)
    min_pad = MIN_MARGIN_INCHES * scale

    raw_w = max(raw_extent.XMax - raw_extent.XMin, 1)
    raw_h = max(raw_extent.YMax - raw_extent.YMin, 1)
    current_pad_x = (buf_w - raw_w) / 2
    current_pad_y = (buf_h - raw_h) / 2

    extra_x = max(0.0, min_pad - current_pad_x)
    extra_y = max(0.0, min_pad - current_pad_y)

    map_frame.camera.setExtent(arcpy.Extent(
        buffered.XMin - extra_x,
        buffered.YMin - extra_y,
        buffered.XMax + extra_x,
        buffered.YMax + extra_y,
    ))


def _get_feature_info(arcpy, hfml_id, feature_class):
    where = _pm_query(hfml_id)
    fields = [HFML_ID_FIELD, "SHAPE@"]
    extents = []
    centroid_points = []
    with arcpy.da.SearchCursor(feature_class, fields, where) as cursor:
        for _, geom in cursor:
            if geom is None:
                continue
            extents.append(geom.extent)
            centroid_points.append(geom.positionAlongLine(0.5, True).firstPoint)

    if not extents:
        raise ValueError(f"No HFML features found for {hfml_id} in {feature_class}")

    extent = _extent_union(arcpy, extents)
    target = centroid_points[0]
    return extent, target


def _collect_extents_by_oid(arcpy, oid_list, source_path, extents):
    if not oid_list:
        return
    query = f"OBJECTID IN ({','.join(map(str, oid_list))})"
    with arcpy.da.SearchCursor(source_path, ["SHAPE@"], query) as cursor:
        for (geom,) in cursor:
            if geom is not None:
                extents.append(geom.extent)


def _get_phase4_feature_info(arcpy, hfml, config):
    pmnum = str(hfml["pmnum"])
    seed_extent, seed_target = _get_feature_info(arcpy, pmnum, config["p4_ml_layer_path"])
    extents = [seed_extent]

    _collect_extents_by_oid(arcpy, hfml.get("ml_oids") or [], config["p4_ml_source_path"], extents)
    _collect_extents_by_oid(arcpy, hfml.get("ll_oids") or [], config["p4_ll_source_path"], extents)
    _collect_extents_by_oid(arcpy, hfml.get("parcel_oids") or [], config["p4_parcels_source_path"], extents)

    return _extent_union(arcpy, extents), seed_target


def _select_template(extent, config):
    width = max(extent.XMax - extent.XMin, 1)
    height = max(extent.YMax - extent.YMin, 1)
    ratio = height / width
    if ratio > 1:
        return VERTICAL_LAYOUT_NAME, "vertical"
    return HORIZONTAL_LAYOUT_NAME, "horizontal"


def _get_single(items, name, label):
    matches = [item for item in items if item.name == name]
    if matches:
        return matches[0]
    if items:
        return items[0]
    raise RuntimeError(f"No {label} found")


def _get_map(aprx):
    maps = aprx.listMaps(MAP_NAME)
    if maps:
        return maps[0]
    maps = aprx.listMaps()
    if not maps:
        raise RuntimeError("No maps found in ArcGIS project")
    return maps[0]


def _get_layout(aprx, layout_name):
    layouts = aprx.listLayouts(layout_name)
    if layouts:
        return layouts[0]
    available = [layout.name for layout in aprx.listLayouts()]
    raise RuntimeError(f"Layout '{layout_name}' not found. Available layouts: {available}")


def _scope_layers(map_obj, hfml_id):
    where = _pm_query(hfml_id)
    trace_names = {
        "Trace_SSGRAVITYMAIN",
        "Trace Upstream High Freq PM",
        "Lower_Laterals",
        "Parcels in the Trace",
        "Parcels_trace",
    }
    for layer in map_obj.listLayers():
        if layer.isGroupLayer or not layer.supports("DEFINITIONQUERY"):
            continue
        if layer.name == HFML_LAYER_NAME:
            layer.definitionQuery = where
        elif layer.name in trace_names:
            # Phase 4 currently writes one PoC at a time. Leave trace layers visible,
            # but this branch is where per-HFML trace scoping should go once Phase 4 batches.
            pass


def _page_xy_from_map_point(map_frame, point):
    extent = map_frame.camera.getExtent()
    if extent is None:
        return None
    width = extent.XMax - extent.XMin
    height = extent.YMax - extent.YMin
    if width == 0 or height == 0:
        return None

    rel_x = (point.X - extent.XMin) / width
    rel_y = (point.Y - extent.YMin) / height
    page_x = map_frame.elementPositionX + rel_x * map_frame.elementWidth
    page_y = map_frame.elementPositionY + rel_y * map_frame.elementHeight
    return page_x, page_y


def _map_extent_to_page(map_frame, map_extent):
    cam = map_frame.camera.getExtent()
    if cam is None:
        return None
    cam_w = max(cam.XMax - cam.XMin, 1)
    cam_h = max(cam.YMax - cam.YMin, 1)
    fx = map_frame.elementPositionX
    fy = map_frame.elementPositionY
    fw = map_frame.elementWidth
    fh = map_frame.elementHeight
    px1 = fx + (map_extent.XMin - cam.XMin) / cam_w * fw
    py1 = fy + (map_extent.YMin - cam.YMin) / cam_h * fh
    px2 = fx + (map_extent.XMax - cam.XMin) / cam_w * fw
    py2 = fy + (map_extent.YMax - cam.YMin) / cam_h * fh
    return px1, py1, px2, py2


def _rects_overlap(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):
    return not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)


def _box_edge_toward(bx1, by1, bx2, by2, tx, ty):
    cx, cy = (bx1 + bx2) / 2, (by1 + by2) / 2
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, by1
    best = None
    for edge_x in (bx1, bx2):
        if dx != 0:
            t = (edge_x - cx) / dx
            if t > 1e-9:
                y = cy + t * dy
                if by1 <= y <= by2:
                    if best is None or t < best[0]:
                        best = (t, edge_x, y)
    for edge_y in (by1, by2):
        if dy != 0:
            t = (edge_y - cy) / dy
            if t > 1e-9:
                x = cx + t * dx
                if bx1 <= x <= bx2:
                    if best is None or t < best[0]:
                        best = (t, x, edge_y)
    return (best[1], best[2]) if best else (cx, by1)


def _best_callout_position(map_frame, feature_extent, target_x, target_y, box_w, box_h, step=0.1):
    fx = map_frame.elementPositionX
    fy = map_frame.elementPositionY
    fw = map_frame.elementWidth
    fh = map_frame.elementHeight
    edge = 0.2

    page_feat = _map_extent_to_page(map_frame, feature_extent)
    fpx1, fpy1, fpx2, fpy2 = page_feat if page_feat else (0, 0, 0, 0)

    x_min = fx + edge
    x_max = fx + fw - edge - box_w
    y_min = fy + edge
    y_max = fy + fh - edge - box_h

    best = None
    bx = x_min
    while bx <= x_max + 1e-9:
        by = y_min
        while by <= y_max + 1e-9:
            overlaps = _rects_overlap(bx, by, bx + box_w, by + box_h, fpx1, fpy1, fpx2, fpy2)
            cx, cy = bx + box_w / 2, by + box_h / 2
            dist = ((cx - target_x) ** 2 + (cy - target_y) ** 2) ** 0.5
            score = (int(overlaps), dist)
            if best is None or score < best[0]:
                best = (score, bx, by)
            by += step
        bx += step

    return (best[1], best[2]) if best else (x_min, y_max)


def _set_text_elements(layout, hfml_id, today):
    for element in layout.listElements("TEXT_ELEMENT"):
        if element.name == DATE_ELEMENT_NAME:
            element.text = today
        elif element.name == FIGURE_ELEMENT_NAME:
            element.text = f"Figure {hfml_id} - Area of Interest\nStop The Clog Strategy"


def _query_available_fields(arcpy, feature_class, wanted, hfml_id, log_fn=None):
    try:
        id_field = _resolve_field(arcpy, feature_class, ["pmnum", HFML_ID_FIELD])
        available = {f.name.lower(): f.name for f in arcpy.ListFields(feature_class)}
        query_fields = [available[w.lower()] for w in wanted if w.lower() in available]
        missing = [w for w in wanted if w.lower() not in available]
        if log_fn:
            log_fn(f"  [{feature_class}] found fields: {query_fields}")
            if missing:
                log_fn(f"  [{feature_class}] missing fields: {missing}")
        if not query_fields:
            return {}
        with arcpy.da.SearchCursor(feature_class, query_fields, _query(id_field, hfml_id)) as cursor:
            for row in cursor:
                result = {k.lower(): v for k, v in zip(query_fields, row)}
                if log_fn:
                    log_fn(f"  [{feature_class}] row values: {result}")
                return result
        if log_fn:
            log_fn(f"  [{feature_class}] query returned no rows for hfml_id={hfml_id} using id_field={id_field}")
    except Exception as exc:
        if log_fn:
            log_fn(f"  [{feature_class}] query error: {exc}")
    return {}


def _get_callout_text(arcpy, config, hfml_id, log_fn=None):
    wanted = [
        "SASD_SSGRAVITYMAIN_GRID_NO",
        "SASD_SSGRAVITYMAIN_DIAMETER",
        "SASD_SSGRAVITYMAIN_MATLTYPE",
        "PM_Mainlines_frequency",
        "PM_Mainlines_laststartdate",
    ]
    if log_fn:
        log_fn(f"Callout field lookup for HFML {hfml_id}:")
    vals = _query_available_fields(arcpy, config["p4_ml_layer_path"], wanted, hfml_id, log_fn)
    fallback = _query_available_fields(arcpy, config["feature_class_path"], wanted, hfml_id, log_fn)
    for k, v in fallback.items():
        if k not in vals or not vals[k]:
            vals[k] = v

    grid_no = vals.get("sasd_ssgravitymain_grid_no") or hfml_id
    diameter = vals.get("sasd_ssgravitymain_diameter") or ""
    mat_type = vals.get("sasd_ssgravitymain_matltype") or ""
    frequency = vals.get("pm_mainlines_frequency") or ""
    last_date = vals.get("pm_mainlines_laststartdate") or ""

    if hasattr(last_date, "strftime"):
        last_date = last_date.strftime("%m/%d/%y")

    pipe_parts = []
    if diameter:
        pipe_parts.append(f'{diameter}"')
    if mat_type:
        pipe_parts.append(str(mat_type))

    lines = [f"ML {grid_no}"]
    if pipe_parts:
        lines.append(" ".join(pipe_parts))
    lines.append("")
    lines.append("Notes:")
    if frequency:
        lines.append(f"PM Freq: {frequency} Month")
    if last_date:
        lines.append(f"Last Hydro: {last_date}")
    lines.append("Observations:")
    return "\n".join(lines)


def _try_add_callout(layout, map_frame, feature_extent, target_point, text, log_fn):
    page_xy = _page_xy_from_map_point(map_frame, target_point)
    if page_xy is None:
        log_fn("Callout skipped: could not convert HFML map point to page coordinates")
        return

    target_x, target_y = page_xy

    try:
        cim = layout.getDefinition("V2")
        elements = list(cim.elements)
        text_element = next((e for e in elements if e.name == "Auto HFML Callout"), None)
        line_element = next((e for e in elements if e.name == "Auto HFML Arrow"), None)
        if text_element is None or line_element is None:
            log_fn("Callout skipped: add 'Auto HFML Callout' (text) and 'Auto HFML Arrow' (line) elements to the layout in ArcGIS Pro to enable the callout box")
            return

        lines = text.split("\n")
        max_chars = max((len(l) for l in lines), default=8)
        box_w = max(0.7, max_chars * 0.042 + 0.15)
        box_h = max(0.5, len(lines) * 0.095 + 0.2)
        box_x1, box_y1 = _best_callout_position(map_frame, feature_extent, target_x, target_y, box_w, box_h)
        box_x2, box_y2 = box_x1 + box_w, box_y1 + box_h
        arrow_sx, arrow_sy = _box_edge_toward(box_x1, box_y1, box_x2, box_y2, target_x, target_y)

        text_rings = [[[box_x1, box_y1], [box_x1, box_y2], [box_x2, box_y2], [box_x2, box_y1], [box_x1, box_y1]]]
        text_element.locked = False
        text_element.visible = True
        text_element.rotationCenter.x = box_x1
        text_element.rotationCenter.y = box_y1
        text_element.graphic.text = text
        if isinstance(text_element.graphic.shape, dict):
            text_element.graphic.shape["rings"] = text_rings
        else:
            text_element.graphic.shape.rings = text_rings
        if getattr(text_element.graphic, "symbol", None):
            sym = text_element.graphic.symbol.symbol
            sym.height = 6.5
            sym.horizontalAlignment = "Left"
            sym.verticalAlignment = "Top"
            sym.offsetX = 6
            sym.offsetY = -6
            try:
                sym.callout = {
                    "type": "CIMBackgroundCallout",
                    "backgroundSymbol": {
                        "type": "CIMSymbolReference",
                        "symbol": {
                            "type": "CIMPolygonSymbol",
                            "symbolLayers": [
                                {"type": "CIMSolidFill", "enable": True,
                                 "color": {"type": "CIMRGBColor", "values": [255, 255, 255, 100]}},
                                {"type": "CIMSolidStroke", "enable": True, "width": 1,
                                 "color": {"type": "CIMRGBColor", "values": [0, 0, 0, 100]}},
                            ],
                        },
                    },
                    "margin": 3,
                }
            except Exception:
                pass

        line_paths = [[[arrow_sx, arrow_sy], [target_x, target_y]]]
        line_element.locked = False
        line_element.visible = True
        line_element.rotationCenter.x = arrow_sx
        line_element.rotationCenter.y = arrow_sy
        if isinstance(line_element.graphic.line, dict):
            line_element.graphic.line["paths"] = line_paths
        else:
            line_element.graphic.line.paths = line_paths

        layout.setDefinition(cim)
        log_fn(f"Added HFML callout at page ({box_x1:.2f}, {box_y1:.2f}), arrow from ({arrow_sx:.2f}, {arrow_sy:.2f})")
    except Exception as exc:
        log_fn(f"Callout placement failed: {exc}")


def _load_phase4_manifest():
    if not os.path.exists(PHASE4_MAP_OUTPUT_PATH):
        raise FileNotFoundError(
            f"Phase 4 map manifest not found: {PHASE4_MAP_OUTPUT_PATH}. "
            "Run Phase 4 successfully before Phase 5."
        )
    with open(PHASE4_MAP_OUTPUT_PATH, "r") as f:
        manifest = json.load(f)
    hfmls = manifest.get("hfmls", [])
    if not isinstance(hfmls, list):
        raise ValueError(f"Invalid Phase 4 map manifest: {PHASE4_MAP_OUTPUT_PATH}")
    return hfmls


def _export_one(arcpy, hfml, config, today, log_fn):
    hfml_id = str(hfml["pmnum"])
    extent, target_point = _get_phase4_feature_info(arcpy, hfml, config)
    layout_name, orientation = _select_template(extent, config)
    log_fn(f"Selected {orientation} layout for HFML {hfml_id}")

    os.makedirs(config["pdf_output_dir"], exist_ok=True)
    aprx_src = os.path.abspath(config["arcgis_project_path"])
    aprx_dir = os.path.dirname(aprx_src)
    tmp_aprx = os.path.join(aprx_dir, f"_phase5_{hfml_id}_tmp.aprx")
    shutil.copy(aprx_src, tmp_aprx)

    try:
        aprx = arcpy.mp.ArcGISProject(tmp_aprx)
        map_obj = _get_map(aprx)
        layout = _get_layout(aprx, layout_name)
        map_frame = _get_single(layout.listElements("MAPFRAME_ELEMENT"), MAP_FRAME_NAME, "map frame")
        map_frame.map = map_obj

        _scope_layers(map_obj, hfml_id)
        _apply_camera_extent(arcpy, map_frame, extent)
        _set_text_elements(layout, hfml_id, today)
        _try_add_callout(
            layout,
            map_frame,
            extent,
            target_point,
            _get_callout_text(arcpy, config, hfml_id, log_fn),
            log_fn,
        )

        aprx.save()
        pdf_path = os.path.join(config["pdf_output_dir"], f"{hfml_id}.pdf")
        layout.exportToPDF(pdf_path)
        log_fn(f"Exported map for HFML {hfml_id} -> {pdf_path}")
    finally:
        try:
            del aprx
        except Exception:
            pass
        try:
            os.remove(tmp_aprx)
        except Exception:
            pass


def run(log_fn):
    arcpy = _import_arcpy()
    config = load_config()

    product_info = arcpy.ProductInfo()
    if product_info in {"NotInitialized", "Unavailable", "Engine", "ArcServer"}:
        raise RuntimeError(f"ArcGIS Pro license not available: {product_info}")
    log_fn(f"ArcGIS product license: {product_info}")

    hfmls = _load_phase4_manifest()

    if not hfmls:
        log_fn("No new HFMLs to process - Phase 5 complete")
        return

    today = date.today().strftime("%B %d, %Y")
    exported = 0
    for hfml in hfmls:
        hfml_id = str(hfml["pmnum"])
        log_fn(f"Generating map for HFML {hfml_id}...")
        _export_one(arcpy, hfml, config, today, log_fn)
        exported += 1

    log_fn(f"Phase 5 complete - {exported} PDFs exported to {config['pdf_output_dir']}")
