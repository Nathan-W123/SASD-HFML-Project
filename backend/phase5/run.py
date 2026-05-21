import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json
import re
import shutil
from datetime import date

import pyodbc

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

MAP_BUFFER = 1.3
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


def _apply_camera_extent(arcpy, map_frame, raw_extent, log_fn=None):
    padded = _buffer_extent(arcpy, raw_extent)

    # Match the padded extent's aspect ratio to the frame so ArcGIS doesn't
    # consume our margins when it fits the extent into the frame.
    frame_ratio = map_frame.elementHeight / max(map_frame.elementWidth, 0.01)
    pad_w = max(padded.XMax - padded.XMin, 1)
    pad_h = max(padded.YMax - padded.YMin, 1)
    cx = (padded.XMin + padded.XMax) / 2
    cy = (padded.YMin + padded.YMax) / 2

    if pad_h / pad_w > frame_ratio:
        new_w = pad_h / frame_ratio
        new_h = pad_h
    else:
        new_w = pad_w
        new_h = pad_w * frame_ratio

    cam_xmin = cx - new_w / 2
    cam_ymin = cy - new_h / 2
    cam_xmax = cx + new_w / 2
    cam_ymax = cy + new_h / 2

    if log_fn:
        log_fn(f"Content extent: X[{raw_extent.XMin:.0f},{raw_extent.XMax:.0f}] Y[{raw_extent.YMin:.0f},{raw_extent.YMax:.0f}]")
        log_fn(f"Frame {map_frame.elementWidth:.2f}w x {map_frame.elementHeight:.2f}h in, ratio={frame_ratio:.3f}")
        log_fn(f"Camera extent: X[{cam_xmin:.0f},{cam_xmax:.0f}] Y[{cam_ymin:.0f},{cam_ymax:.0f}] ({new_w:.0f}w x {new_h:.0f}h map units)")

    map_frame.camera.setExtent(arcpy.Extent(cam_xmin, cam_ymin, cam_xmax, cam_ymax))


def _get_feature_info(arcpy, hfml_id, feature_class):
    where = _pm_query(hfml_id)
    fields = [HFML_ID_FIELD, "SHAPE@"]
    extents = []
    longest_len = -1
    target = None
    with arcpy.da.SearchCursor(feature_class, fields, where) as cursor:
        for _, geom in cursor:
            if geom is None:
                continue
            extents.append(geom.extent)
            # Use the midpoint of the longest segment so short stub segments
            # don't pull the arrow target to a fringe end of the feature.
            if geom.length > longest_len:
                longest_len = geom.length
                target = geom.positionAlongLine(0.5, True).firstPoint

    if not extents:
        raise ValueError(f"No HFML features found for {hfml_id} in {feature_class}")

    extent = _extent_union(arcpy, extents)
    return extent, target


def _collect_extents_by_oid(arcpy, oid_list, source_path, extents):
    if not oid_list:
        return
    query = f"OBJECTID IN ({','.join(map(str, oid_list))})"
    with arcpy.da.SearchCursor(source_path, ["SHAPE@"], query) as cursor:
        for (geom,) in cursor:
            if geom is not None:
                extents.append(geom.extent)


def _get_phase4_feature_info(arcpy, hfml, config, log_fn=None):
    pmnum = str(hfml["pmnum"])
    extents = []
    seed_target = None

    try:
        seed_extent, seed_target = _get_feature_info(arcpy, pmnum, config["p4_ml_layer_path"])
        extents.append(seed_extent)
        if log_fn:
            log_fn(f"  Seed extent from HFML layer: X[{seed_extent.XMin:.0f},{seed_extent.XMax:.0f}]")
    except ValueError:
        # New HFML not in PMs_SSGRAVITYMAIN — look up its own geometry via PM_Mainlines -> assetnum -> SASD_SSGRAVITYMAIN
        assetnum = None
        try:
            with arcpy.da.SearchCursor(
                config["feature_class_path"], ["assetnum"],
                f"pmnum = '{_sql_literal(pmnum)}'"
            ) as cur:
                for (a,) in cur:
                    assetnum = str(a).strip() if a else None
                    break
        except Exception:
            pass

        if assetnum:
            try:
                with arcpy.da.SearchCursor(
                    config["p4_ml_source_path"], ["SHAPE@"],
                    f"MXASSETNUM = '{_sql_literal(assetnum)}'"
                ) as cur:
                    for (geom,) in cur:
                        if geom:
                            extents.append(geom.extent)
                            seed_target = geom.positionAlongLine(0.5, True).firstPoint
                        break
            except Exception:
                pass

    pre_ml = len(extents)
    _collect_extents_by_oid(arcpy, hfml.get("ml_oids") or [], config["p4_ml_source_path"], extents)
    pre_ll = len(extents)
    _collect_extents_by_oid(arcpy, hfml.get("ll_oids") or [], config["p4_ll_source_path"], extents)
    pre_parcel = len(extents)
    _collect_extents_by_oid(arcpy, hfml.get("parcel_oids") or [], config["p4_parcels_source_path"], extents)
    if log_fn:
        log_fn(f"  Extent sources — ml:{pre_ll-pre_ml} ll:{pre_parcel-pre_ll} parcels:{len(extents)-pre_parcel} total:{len(extents)}")

    if not extents:
        raise ValueError(f"No geometry found for HFML {pmnum}")

    union = _extent_union(arcpy, extents)

    if seed_target is None:
        ml_oids = hfml.get("ml_oids") or []
        if ml_oids:
            with arcpy.da.SearchCursor(
                config["p4_ml_source_path"], ["SHAPE@"],
                f"OBJECTID = {ml_oids[0]}"
            ) as cursor:
                for (geom,) in cursor:
                    if geom:
                        seed_target = geom.positionAlongLine(0.5, True).firstPoint
                    break
        if seed_target is None:
            seed_target = arcpy.Point(
                (union.XMin + union.XMax) / 2,
                (union.YMin + union.YMax) / 2,
            )

    return union, seed_target


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


def _scope_layers(arcpy, map_obj, hfml_id, content_extent=None, log_fn=None):
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
            hfml_where = f"PM_Mainlines_pmnum = '{_sql_literal(hfml_id)}'"
            try:
                count = sum(1 for _ in arcpy.da.SearchCursor(layer, ["OID@"], hfml_where))
            except Exception:
                count = 0
            layer.definitionQuery = hfml_where if count > 0 else ""
        elif layer.name in trace_names and content_extent is not None:
            # Filter to only the features visible in the padded map view.
            # Uses content_extent which should be the camera extent (padded) so
            # features at the map margin aren't excluded from the definition query.
            try:
                sr = arcpy.Describe(layer.dataSource).spatialReference
                ext_poly = arcpy.Polygon(
                    arcpy.Array([
                        arcpy.Point(content_extent.XMin, content_extent.YMin),
                        arcpy.Point(content_extent.XMin, content_extent.YMax),
                        arcpy.Point(content_extent.XMax, content_extent.YMax),
                        arcpy.Point(content_extent.XMax, content_extent.YMin),
                        arcpy.Point(content_extent.XMin, content_extent.YMin),
                    ]),
                    sr,
                )
                arcpy.management.SelectLayerByLocation(layer, "INTERSECT", ext_poly)
                oids = [r[0] for r in arcpy.da.SearchCursor(layer, ["OID@"])]
                arcpy.management.SelectLayerByAttribute(layer, "CLEAR_SELECTION")
                layer.definitionQuery = (
                    f"OBJECTID IN ({','.join(map(str, oids))})" if oids else "1=0"
                )
            except Exception as _scope_exc:
                # On failure reset to show all features rather than leaving a
                # stale definition query from a previous HFML's export.
                try:
                    layer.definitionQuery = ""
                except Exception:
                    pass
                if log_fn:
                    log_fn(f"  WARNING: could not filter layer '{layer.name}' — showing all features: {_scope_exc}")


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


def _best_callout_position(map_frame, feature_extent, target_x, target_y, box_w, box_h):
    fx = map_frame.elementPositionX
    fy = map_frame.elementPositionY
    fw = map_frame.elementWidth
    fh = map_frame.elementHeight
    edge = 0.15

    page_feat = _map_extent_to_page(map_frame, feature_extent)
    fpx1, fpy1, fpx2, fpy2 = page_feat if page_feat else (0, 0, 0, 0)

    x_min = fx + edge
    x_max = fx + fw - edge - box_w
    y_min = fy + edge
    y_max = fy + fh - edge - box_h

    def _c(v, lo, hi):
        return max(lo, min(hi, v))

    # 0.25in minimum gap between callout and content extent
    gap = 0.25
    cont_cx = (fpx1 + fpx2) / 2
    cont_cy = (fpy1 + fpy2) / 2

    # Candidate positions placed at exactly the gap distance from each side of the content
    candidates = [
        # Right of content — vertically centered on content, then top/bottom
        (_c(fpx2 + gap, x_min, x_max), _c(cont_cy - box_h / 2, y_min, y_max)),
        (_c(fpx2 + gap, x_min, x_max), y_max),
        (_c(fpx2 + gap, x_min, x_max), y_min),
        # Left of content
        (_c(fpx1 - gap - box_w, x_min, x_max), _c(cont_cy - box_h / 2, y_min, y_max)),
        (_c(fpx1 - gap - box_w, x_min, x_max), y_max),
        (_c(fpx1 - gap - box_w, x_min, x_max), y_min),
        # Above content — horizontally centered, then sides
        (_c(cont_cx - box_w / 2, x_min, x_max), _c(fpy2 + gap, y_min, y_max)),
        (x_min, _c(fpy2 + gap, y_min, y_max)),
        (x_max, _c(fpy2 + gap, y_min, y_max)),
        # Below content
        (_c(cont_cx - box_w / 2, x_min, x_max), _c(fpy1 - gap - box_h, y_min, y_max)),
        (x_min, _c(fpy1 - gap - box_h, y_min, y_max)),
        (x_max, _c(fpy1 - gap - box_h, y_min, y_max)),
        # Frame corners as final fallback
        (x_min, y_min), (x_max, y_min), (x_min, y_max), (x_max, y_max),
    ]

    best = None
    for bx, by in candidates:
        overlaps = _rects_overlap(bx, by, bx + box_w, by + box_h, fpx1, fpy1, fpx2, fpy2)
        # Score: prefer no overlap, then minimize distance to target (shortest arrow)
        cx, cy = bx + box_w / 2, by + box_h / 2
        target_dist = ((cx - target_x) ** 2 + (cy - target_y) ** 2) ** 0.5
        score = (int(overlaps), target_dist)
        if best is None or score < best[0]:
            best = (score, bx, by)

    return (best[1], best[2]) if best else (x_min, y_max)


def _lookup_gridno(arcpy, config, hfml_id):
    """Return the gridno for this PM, falling back to hfml_id if not found."""
    try:
        available = {f.name.lower(): f.name for f in arcpy.ListFields(config["feature_class_path"])}
        if "gridno" not in available:
            return hfml_id
        with arcpy.da.SearchCursor(
            config["feature_class_path"], [available["gridno"]],
            f"pmnum = '{_sql_literal(hfml_id)}'"
        ) as cur:
            for (val,) in cur:
                return str(val) if val else hfml_id
    except Exception:
        pass
    return hfml_id


def _set_text_elements(layout, hfml_id, gridno, today, log_fn=None):
    updated = []
    for element in layout.listElements("TEXT_ELEMENT"):
        if element.name == DATE_ELEMENT_NAME:
            element.text = today
            updated.append(DATE_ELEMENT_NAME)
        elif element.name == FIGURE_ELEMENT_NAME:
            element.text = f"Figure {gridno} - Area of Interest\nStop The Clog Strategy"
            updated.append(FIGURE_ELEMENT_NAME)
    if log_fn:
        all_names = [e.name for e in layout.listElements("TEXT_ELEMENT")]
        log_fn(f"Text elements in layout: {all_names}")
        log_fn(f"Text elements updated: {updated}")



def _get_callout_text(arcpy, config, hfml_id, observations="", log_fn=None, gridno=None):
    # Step 1: pull PM attributes directly from PM_Mainlines.
    # Accept a pre-fetched gridno to avoid a redundant lookup.
    frequency = ""
    last_date = ""
    assetnum = ""
    gridno = gridno or hfml_id

    pm_available = {f.name.lower(): f.name for f in arcpy.ListFields(config["feature_class_path"])}
    pm_fields = [pm_available[c] for c in ["pmnum", "frequency", "laststartdate", "assetnum", "gridno"] if c in pm_available]
    try:
        with arcpy.da.SearchCursor(
            config["feature_class_path"], pm_fields,
            f"pmnum = '{_sql_literal(hfml_id)}'"
        ) as cursor:
            for row in cursor:
                rd = {k.lower(): v for k, v in zip(pm_fields, row)}
                frequency = str(rd.get("frequency") or "")
                last_date = rd.get("laststartdate") or ""
                assetnum  = str(rd.get("assetnum") or "")
                gridno    = str(rd.get("gridno") or gridno)
                break
    except Exception as exc:
        if log_fn:
            log_fn(f"PM_Mainlines lookup failed for {hfml_id}: {exc}")

    # Step 2: pull pipe attributes from SASD_SSGRAVITYMAIN by MXASSETNUM
    grid_no  = gridno
    diameter = ""
    mat_type = ""

    if assetnum:
        ml_available = {f.name.lower(): f.name for f in arcpy.ListFields(config["p4_ml_source_path"])}
        diam_key = next((c for c in ["diameter", "pipe_diam", "pipediam"] if c in ml_available), None)
        matl_key = next((c for c in ["matltype", "material", "pipematerial", "pipe_matl"] if c in ml_available), None)
        grid_key = next((c for c in ["grid_no", "gridno"] if c in ml_available), None)
        ml_fields = ["mxassetnum"] + [ml_available[k] for k in [diam_key, matl_key, grid_key] if k]
        try:
            with arcpy.da.SearchCursor(
                config["p4_ml_source_path"], ml_fields,
                f"MXASSETNUM = '{_sql_literal(assetnum)}'"
            ) as cursor:
                for row in cursor:
                    rd = {k.lower(): v for k, v in zip(ml_fields, row)}
                    if diam_key:
                        diameter = str(rd.get(diam_key) or "")
                    if matl_key:
                        mat_type = str(rd.get(matl_key) or "")
                    if grid_key:
                        grid_no  = str(rd.get(grid_key) or gridno)
                    break
        except Exception as exc:
            if log_fn:
                log_fn(f"SASD_SSGRAVITYMAIN lookup failed for assetnum {assetnum}: {exc}")

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
    lines.append(f"Observations: {observations}" if observations else "Observations: None")
    text = "\n".join(lines)
    if log_fn:
        log_fn(f"Callout text for {hfml_id}: {repr(text)}")
    return text


def _try_add_callout(layout, map_frame, feature_extent, target_point, text, log_fn):
    import math

    page_xy = _page_xy_from_map_point(map_frame, target_point)
    if page_xy is None:
        log_fn("Callout skipped: could not convert HFML map point to page coordinates")
        return

    target_x, target_y = page_xy

    text_els = layout.listElements("TEXT_ELEMENT", "Auto HFML Callout")
    line_els = layout.listElements("GRAPHIC_ELEMENT", "Auto HFML Arrow")
    text_el = text_els[0] if text_els else None
    line_el = line_els[0] if line_els else None

    if text_el is None or line_el is None:
        log_fn("Callout skipped: add 'Auto HFML Callout' (text) and 'Auto HFML Arrow' (line) elements to the layout in ArcGIS Pro to enable the callout box")
        return

    try:
        lines = text.split("\n")
        max_chars = max((len(l) for l in lines), default=8)
        # 0.07in/char + 0.3in padding avoids wrapping; 0.14in/line + 0.35in margin avoids cutoff
        box_w = max(1.2, max_chars * 0.07 + 0.3)
        box_h = max(0.7, len(lines) * 0.14 + 0.35)
        box_x1, box_y1 = _best_callout_position(map_frame, feature_extent, target_x, target_y, box_w, box_h)
        box_x2, box_y2 = box_x1 + box_w, box_y1 + box_h
        # Arrow touches the textbox edge (not center, not beyond)
        arrow_tx, arrow_ty = _box_edge_toward(box_x1, box_y1, box_x2, box_y2, target_x, target_y)

        # Size and position the text box.
        # ArcGIS Pro text element anchor = TOP-LEFT: elementPositionX is the left
        # edge, elementPositionY is the TOP edge (element extends downward from there).
        text_el.text = text
        text_el.elementWidth = box_w
        text_el.elementHeight = box_h
        text_el.elementPositionX = box_x1
        text_el.elementPositionY = box_y1 + box_h  # top edge
        text_el.visible = True

        # Arrow — use element-level CIM to set exact start/end coordinates.
        # Path: HFML -> textbox edge (arrowhead at HFML, tail stops at box border).
        try:
            line_cim = line_el.getDefinition("V2")
            paths = [[[target_x, target_y], [arrow_tx, arrow_ty]]]
            gfx = line_cim.graphic
            if isinstance(gfx, dict):
                line_obj = gfx.get("line") or gfx.get("polyline", {})
                if isinstance(line_obj, dict):
                    line_obj["paths"] = paths
                else:
                    gfx["line"] = {"paths": paths}
            else:
                line_obj = getattr(gfx, "line", None) or getattr(gfx, "polyline", None)
                if isinstance(line_obj, dict):
                    line_obj["paths"] = paths
                elif line_obj is not None:
                    line_obj.paths = paths
            line_el.setDefinition(line_cim)
            line_el.visible = True
            log_fn(f"Arrow set HFML({target_x:.2f},{target_y:.2f}) -> box edge({arrow_tx:.2f},{arrow_ty:.2f})")
        except Exception as arrow_exc:
            log_fn(f"Arrow CIM failed: {arrow_exc} — hiding arrow")
            line_el.visible = False

        log_fn(f"Callout at page ({box_x1:.2f},{box_y1:.2f}) {box_w:.2f}w x {box_h:.2f}h")
    except Exception as exc:
        log_fn(f"Callout placement failed: {exc}")


_SEVERITIES   = ["heavy", "moderate"]
_DEBRIS_TYPES = ["grease", "solids", "roots"]


def _parse_observations(memo):
    if not memo:
        return ""
    memo_lower = memo.lower()
    findings = []
    for debris in _DEBRIS_TYPES:
        if debris not in memo_lower:
            continue
        pos = memo_lower.index(debris)
        # Search 60 chars before AND after the debris word so both
        # "heavy grease" and "grease (heavy)" / "roots — moderate" are caught.
        window = memo_lower[max(0, pos - 60): pos + len(debris) + 60]
        matched_severity = None
        for sev in _SEVERITIES:
            if re.search(rf"\b{sev}\b", window):
                matched_severity = sev
        if matched_severity:
            findings.append(f"{matched_severity.capitalize()} {debris}")
        else:
            findings.append(debris.capitalize())
    return ", ".join(findings)


def _fetch_observations(pmnum_list, config):
    if not pmnum_list:
        return {}
    conn_str = (
        f"Driver={{SQL Server}};"
        f"Server={config['sql_server']};"
        f"Database={config['sql_database']};"
        f"Trusted_Connection=yes;"
    )
    placeholders = ",".join("?" * len(pmnum_list))
    # Try dbo schema prefix first; fall back to bare table name if the login's
    # default schema differs.  Both failures are logged so silent blank callouts
    # can be diagnosed.
    observations = {}
    for table_ref in ("dbo.pmmemo", "pmmemo"):
        query = f"""
            SELECT pmnum, pmmemo
            FROM (
                SELECT pmnum, pmmemo,
                       ROW_NUMBER() OVER (PARTITION BY pmnum ORDER BY changedate DESC) AS rn
                FROM {table_ref}
                WHERE pmnum IN ({placeholders})
            ) t
            WHERE rn = 1
        """
        try:
            conn = pyodbc.connect(conn_str)
            cur = conn.cursor()
            cur.execute(query, pmnum_list)
            for row in cur.fetchall():
                pmnum = str(row[0]).strip()
                observations[pmnum] = _parse_observations(row[1])
            conn.close()
            break  # succeeded — don't try the fallback
        except Exception as exc:
            if table_ref == "pmmemo":
                pass  # both attempts failed; callout renders without observations
    return observations


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


def _export_one(arcpy, hfml, config, today, observations_map, log_fn):
    hfml_id = str(hfml["pmnum"])
    extent, target_point = _get_phase4_feature_info(arcpy, hfml, config, log_fn)
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

        # Clear any selections carried over from the user's ArcGIS Pro session
        for _lyr in map_obj.listLayers():
            if not _lyr.isGroupLayer:
                try:
                    arcpy.management.SelectLayerByAttribute(_lyr, "CLEAR_SELECTION")
                except Exception:
                    pass

        layout = _get_layout(aprx, layout_name)
        map_frame = _get_single(layout.listElements("MAPFRAME_ELEMENT"), MAP_FRAME_NAME, "map frame")
        map_frame.map = map_obj

        # Set the camera first so we can pass the padded camera extent to
        # _scope_layers — using the unpadded content extent would exclude features
        # that are visible at the map margin from the trace layer definition queries.
        map_obj.referenceScale = 0  # disable scale-dependent symbol sizing
        _apply_camera_extent(arcpy, map_frame, extent, log_fn)
        _scope_layers(arcpy, map_obj, hfml_id, map_frame.camera.getExtent(), log_fn)
        gridno = _lookup_gridno(arcpy, config, hfml_id)
        _set_text_elements(layout, hfml_id, gridno, today, log_fn)
        _try_add_callout(
            layout,
            map_frame,
            extent,
            target_point,
            _get_callout_text(arcpy, config, hfml_id, observations_map.get(hfml_id, ""), log_fn, gridno=gridno),
            log_fn,
        )

        pdf_path = os.path.abspath(os.path.join(config["pdf_output_dir"], f"{hfml_id}.pdf"))
        if os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception:
                pass
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

    pmnum_list = [str(h["pmnum"]) for h in hfmls]
    log_fn(f"Fetching Maximo memo observations for {len(pmnum_list)} PMs...")
    observations_map = _fetch_observations(pmnum_list, config)
    log_fn(f"Observations resolved for {len(observations_map)} PMs")

    exported = 0
    failed   = []
    for hfml in hfmls:
        hfml_id = str(hfml["pmnum"])
        log_fn(f"Generating map for HFML {hfml_id}...")
        try:
            _export_one(arcpy, hfml, config, today, observations_map, log_fn)
            exported += 1
        except Exception as _exp_exc:
            log_fn(f"  ERROR: export failed for HFML {hfml_id} — {_exp_exc}")
            failed.append(hfml_id)

    if failed:
        log_fn(f"Phase 5 complete — {exported} exported, {len(failed)} failed: {failed}")
    else:
        log_fn(f"Phase 5 complete — {exported} PDFs exported to {config['pdf_output_dir']}")
