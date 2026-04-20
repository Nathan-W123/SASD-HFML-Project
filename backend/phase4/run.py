import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json

from config_loader import load_config

# TODO: verify these layer/dataset names match the ArcGIS project
ML_LAYER        = "High Freq ML PMs"
ML_SOURCE       = "MLs not in Upstream High Freq PM"
LL_SOURCE       = "TODO: verify LL source layer name"
PARCELS_SOURCE  = "TODO: verify Parcels source layer name"
ML_DEST         = "Trace Upstream High Freq PM"
LL_DEST         = "TODO: verify LL destination dataset name"
PARCELS_DEST    = "TODO: verify Parcels destination dataset name"

TEMP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "temp", "added_mls.json")


def _build_blacklist(log_fn):
    blacklist = set()
    with arcpy.da.SearchCursor(ML_LAYER, ["SHAPE@"]) as cursor:
        for row in cursor:
            geom = row[0]
            fp = (round(geom.firstPoint.X, 3), round(geom.firstPoint.Y, 3),
                  round(geom.lastPoint.X, 3), round(geom.lastPoint.Y, 3))
            blacklist.add(fp)
            blacklist.add((fp[2], fp[3], fp[0], fp[1]))
    return blacklist


def _build_network(log_fn):
    network = {}
    geoms = {}
    with arcpy.da.SearchCursor(ML_SOURCE, ["SHAPE@", "OBJECTID"]) as cursor:
        for geom, oid in cursor:
            start = (round(geom.firstPoint.X, 3), round(geom.firstPoint.Y, 3))
            end   = (round(geom.lastPoint.X, 3),  round(geom.lastPoint.Y, 3))
            geoms[oid] = (start[0], start[1], end[0], end[1])
            network.setdefault(end, []).append((oid, start))
    return network, geoms


def _trace_upstream(ml_id, network, geoms, blacklist, log_fn):
    arcpy.management.SelectLayerByAttribute(ML_LAYER, "NEW_SELECTION", f"{arcpy.AddFieldDelimiters(ML_LAYER, 'OBJECTID')} = {ml_id}")
    with arcpy.da.SearchCursor(ML_LAYER, ["SHAPE@"]) as cursor:
        start_geom = next(cursor)[0]
    search_pt = (round(start_geom.firstPoint.X, 3), round(start_geom.firstPoint.Y, 3))

    to_append = []
    visited   = set()
    stack     = [search_pt]

    while stack:
        pt = stack.pop()
        for feat_id, upstream_pt in network.get(pt, []):
            if feat_id in visited:
                continue
            visited.add(feat_id)
            if geoms[feat_id] in blacklist:
                continue
            to_append.append(feat_id)
            stack.append(upstream_pt)

    return to_append


def _append_by_oids(oid_list, source, destination):
    if not oid_list:
        return
    query = f"OBJECTID IN ({','.join(map(str, oid_list))})"
    arcpy.management.SelectLayerByAttribute(source, "NEW_SELECTION", query)
    arcpy.management.MakeFeatureLayer(source, "TmpSubset")
    arcpy.management.Append("TmpSubset", destination, "NO_TEST")
    arcpy.management.Delete("TmpSubset")


def _select_lls_contacting_mls(ml_oid_list):
    # Select the appended ML features, then select all LLs that contact them
    query = f"OBJECTID IN ({','.join(map(str, ml_oid_list))})"
    arcpy.management.SelectLayerByAttribute(ML_DEST, "NEW_SELECTION", query)
    arcpy.management.SelectLayerByLocation(
        LL_SOURCE, "BOUNDARY_TOUCHES", ML_DEST, selection_type="NEW_SELECTION"
    )
    ll_oids = [row[0] for row in arcpy.da.SearchCursor(LL_SOURCE, ["OBJECTID"])]
    arcpy.management.SelectLayerByAttribute(LL_SOURCE, "CLEAR_SELECTION")
    arcpy.management.SelectLayerByAttribute(ML_DEST, "CLEAR_SELECTION")
    return ll_oids


def _select_parcels_intersecting_lls(ll_oid_list):
    # Select the appended LL features, then select all parcels that intersect them
    query = f"OBJECTID IN ({','.join(map(str, ll_oid_list))})"
    arcpy.management.SelectLayerByAttribute(LL_DEST, "NEW_SELECTION", query)
    arcpy.management.SelectLayerByLocation(
        PARCELS_SOURCE, "INTERSECT", LL_DEST, selection_type="NEW_SELECTION"
    )
    parcel_oids = [row[0] for row in arcpy.da.SearchCursor(PARCELS_SOURCE, ["OBJECTID"])]
    arcpy.management.SelectLayerByAttribute(PARCELS_SOURCE, "CLEAR_SELECTION")
    arcpy.management.SelectLayerByAttribute(LL_DEST, "CLEAR_SELECTION")
    return parcel_oids


def run(log_fn):
    import arcpy

    config = load_config()

    if not os.path.exists(TEMP_PATH):
        raise FileNotFoundError("added_mls.json not found — run Phase 2 first")

    with open(TEMP_PATH, "r") as f:
        added_ml_ids = json.load(f)

    if not added_ml_ids:
        log_fn("No new HFMLs to process — Phase 4 complete")
        return

    log_fn("Indexing network and building geometry blacklist...")
    blacklist      = _build_blacklist(log_fn)
    network, geoms = _build_network(log_fn)

    for ml_id in added_ml_ids:
        log_fn(f"Processing HFML {ml_id}...")

        # 1. Trace and append all upstream MLs recursively
        ml_oids = _trace_upstream(ml_id, network, geoms, blacklist, log_fn)
        _append_by_oids(ml_oids, ML_SOURCE, ML_DEST)
        log_fn(f"  Appended {len(ml_oids)} upstream ML segments")

        # 2. Select all LLs that contact the appended MLs, then append them
        # TODO: confirm BOUNDARY_TOUCHES is the correct spatial relationship for LLs contacting MLs
        ll_oids = _select_lls_contacting_mls(ml_oids)
        _append_by_oids(ll_oids, LL_SOURCE, LL_DEST)
        log_fn(f"  Appended {len(ll_oids)} LLs contacting upstream MLs")

        # 3. Select all parcels that intersect the appended LLs, then append them
        parcel_oids = _select_parcels_intersecting_lls(ll_oids)
        _append_by_oids(parcel_oids, PARCELS_SOURCE, PARCELS_DEST)
        log_fn(f"  Appended {len(parcel_oids)} parcels intersecting LLs")

    log_fn(f"Phase 4 complete — {len(added_ml_ids)} HFMLs processed")
