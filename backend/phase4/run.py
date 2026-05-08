import os
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json

from config_loader import load_config

BUFFER_DISTANCE = "50000 Feet"
LL_SEARCH_DISTANCE = "1 Foot"
ARCPY_RETRY_ATTEMPTS = 3
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADDED_MLS_PATH = os.path.join(PROJECT_ROOT, "temp", "added_mls.json")
PHASE4_MAP_OUTPUT_PATH = os.path.join(PROJECT_ROOT, "temp", "phase4_mapped_hfmls.json")


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


def _build_blacklist(arcpy, ml_layer):
    blacklist = set()
    with arcpy.da.SearchCursor(ml_layer, ["SHAPE@"]) as cursor:
        for row in cursor:
            geom = row[0]
            if geom is None:
                continue
            fp = (round(geom.firstPoint.X, 3), round(geom.firstPoint.Y, 3),
                  round(geom.lastPoint.X, 3), round(geom.lastPoint.Y, 3))
            blacklist.add(fp)
            blacklist.add((fp[2], fp[3], fp[0], fp[1]))
    return blacklist


def _clear_workspace_cache(arcpy):
    try:
        arcpy.management.ClearWorkspaceCache()
    except Exception:
        pass


def _with_arcpy_retry(arcpy, action, context):
    last_exc = None
    for attempt in range(1, ARCPY_RETRY_ATTEMPTS + 1):
        try:
            return action()
        except Exception as exc:
            last_exc = exc
            if attempt == ARCPY_RETRY_ATTEMPTS:
                break
            _clear_workspace_cache(arcpy)
            time.sleep(attempt)
    raise RuntimeError(
        f"{context} failed after {ARCPY_RETRY_ATTEMPTS} attempts. Last error: {last_exc}"
    ) from last_exc


def _replace_temp_layer(arcpy, layer_name, source, where_clause=None):
    def _make_layer():
        if arcpy.Exists(layer_name):
            arcpy.management.Delete(layer_name)
        arcpy.management.MakeFeatureLayer(source, layer_name, where_clause)
        return layer_name

    return _with_arcpy_retry(arcpy, _make_layer, f"Failed to open feature layer from {source}")


def _get_field_names(arcpy, dataset):
    return _with_arcpy_retry(
        arcpy,
        lambda: [field.name for field in arcpy.ListFields(dataset)],
        f"Failed to list fields for {dataset}",
    )


def _geom_to_temp_fc(arcpy, geom, name):
    path = f"in_memory\\{name}"
    if arcpy.Exists(path):
        arcpy.management.Delete(path)
    arcpy.management.CreateFeatureclass("in_memory", name, "POLYLINE", spatial_reference=geom.spatialReference)
    with arcpy.da.InsertCursor(path, ["SHAPE@"]) as cursor:
        cursor.insertRow([geom])
    return path




def _require_dataset(arcpy, path, label, log_fn):
    exists = False
    for attempt in range(1, ARCPY_RETRY_ATTEMPTS + 1):
        try:
            exists = bool(arcpy.Exists(path))
        except Exception:
            exists = False
        if exists:
            break
        if attempt < ARCPY_RETRY_ATTEMPTS:
            _clear_workspace_cache(arcpy)
            time.sleep(attempt)
    if not exists:
        raise RuntimeError(f"{label} does not exist: {path}")
    log_fn(f"{label}: {path}")


def _require_write_lock(arcpy, path, label):
    if not arcpy.TestSchemaLock(path):
        raise RuntimeError(
            f"{label} is locked and cannot be written: {path}. "
            "Close ArcGIS Pro maps, attribute tables, edit sessions, Catalog previews, "
            "or any other tools using this geodatabase, then rerun Phase 4."
        )


def _preflight_phase4(arcpy, config, log_fn):
    dataset_labels = [
        ("ML source", config["p4_ml_source_path"]),
        ("ML destination", config["p4_ml_dest_path"]),
        ("LL source", config["p4_ll_source_path"]),
        ("LL destination", config["p4_ll_dest_path"]),
        ("Parcels source", config["p4_parcels_source_path"]),
        ("Parcels destination", config["p4_parcels_dest_path"]),
    ]
    for label, path in dataset_labels:
        _require_dataset(arcpy, path, label, log_fn)

    writable_targets = [
        ("ML destination", config["p4_ml_dest_path"]),
        ("LL destination", config["p4_ll_dest_path"]),
        ("Parcels destination", config["p4_parcels_dest_path"]),
    ]
    for label, path in writable_targets:
        _require_write_lock(arcpy, path, label)
    log_fn("Phase 4 destination write locks available")

    ml_source_fields = _get_field_names(arcpy, config["p4_ml_source_path"])
    if "OBJECTID" not in ml_source_fields:
        raise RuntimeError(
            f"ML source is missing OBJECTID, which Phase 4 tracing requires: {config['p4_ml_source_path']}"
        )
    log_fn(f"LL source field count: {len(_get_field_names(arcpy, config['p4_ll_source_path']))}")
    log_fn(f"LL destination field count: {len(_get_field_names(arcpy, config['p4_ll_dest_path']))}")
    log_fn(f"Parcels source field count: {len(_get_field_names(arcpy, config['p4_parcels_source_path']))}")
    log_fn(f"Parcels destination field count: {len(_get_field_names(arcpy, config['p4_parcels_dest_path']))}")


def _build_network_from_source(arcpy, ml_source, start_geom, log_fn):
    log_fn(f"Spatially filtering network within {BUFFER_DISTANCE} of target HFML...")
    seed_fc = _geom_to_temp_fc(arcpy, start_geom, "_SeedGeomNet")
    _replace_temp_layer(arcpy, "_NetworkSource", ml_source)
    arcpy.management.SelectLayerByLocation(
        "_NetworkSource", "WITHIN_A_DISTANCE", seed_fc,
        search_distance=BUFFER_DISTANCE,
        selection_type="NEW_SELECTION"
    )
    arcpy.management.Delete(seed_fc)

    network = {}
    geoms = {}
    with arcpy.da.SearchCursor("_NetworkSource", ["SHAPE@", "OBJECTID"]) as cursor:
        for geom, oid in cursor:
            if geom is None:
                continue
            start = (round(geom.firstPoint.X, 3), round(geom.firstPoint.Y, 3))
            end   = (round(geom.lastPoint.X, 3),  round(geom.lastPoint.Y, 3))
            geoms[oid] = (start[0], start[1], end[0], end[1])
            network.setdefault(end, []).append((oid, start))

    arcpy.management.Delete("_NetworkSource")
    log_fn(f"Network built from {len(geoms)} features in filtered area")
    return network, geoms


def _make_seed_ml_layer_from_geom(arcpy, ml_source, start_geom):
    seed_fc = _geom_to_temp_fc(arcpy, start_geom, "_SeedGeomLayer")
    layer = _replace_temp_layer(arcpy, "_SeedHFML", ml_source)
    arcpy.management.SelectLayerByLocation(layer, "INTERSECT", seed_fc, selection_type="NEW_SELECTION")
    arcpy.management.Delete(seed_fc)
    return layer


def _trace_upstream(start_geom, network, geoms, blacklist, log_fn=None):
    search_pt = (round(start_geom.firstPoint.X, 3), round(start_geom.firstPoint.Y, 3))
    if log_fn:
        log_fn(f"Trace start point: {search_pt}, network hits: {len(network.get(search_pt, []))}")

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


def _append_by_oids(arcpy, oid_list, source, destination):
    if not oid_list:
        return
    query = f"OBJECTID IN ({','.join(map(str, oid_list))})"
    subset_layer = _replace_temp_layer(arcpy, "_TmpSubset", source, query)
    try:
        _with_arcpy_retry(
            arcpy,
            lambda: arcpy.management.Append(subset_layer, destination, "NO_TEST"),
            f"Failed to append from {source} to {destination}",
        )
    finally:
        if arcpy.Exists(subset_layer):
            arcpy.management.Delete(subset_layer)


def _preview_oids(oid_list, limit=10):
    if not oid_list:
        return "[]"
    preview = ", ".join(map(str, oid_list[:limit]))
    if len(oid_list) > limit:
        preview += ", ..."
    return f"[{preview}]"


def _write_phase4_map_manifest(hfml_records, log_fn):
    manifest = {"version": 1, "source": "phase4", "hfmls": hfml_records}
    os.makedirs(os.path.dirname(PHASE4_MAP_OUTPUT_PATH), exist_ok=True)
    with open(PHASE4_MAP_OUTPUT_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    log_fn(f"Phase 4 map manifest written: {PHASE4_MAP_OUTPUT_PATH}")


def _build_ll_endpoint_table(arcpy, ll_layer, output_name="_TmpLLEndpoints"):
    output_path = f"in_memory\\{output_name}"
    if arcpy.Exists(output_path):
        arcpy.management.Delete(output_path)

    ll_desc = arcpy.Describe(ll_layer)
    spatial_ref = ll_desc.spatialReference
    arcpy.management.CreateFeatureclass("in_memory", output_name, "POINT", spatial_reference=spatial_ref)
    arcpy.management.AddField(output_path, "LL_OID", "LONG")
    arcpy.management.AddField(output_path, "END_ID", "LONG")

    end_id = 1
    with arcpy.da.InsertCursor(output_path, ["SHAPE@", "LL_OID", "END_ID"]) as insert_cursor:
        with arcpy.da.SearchCursor(ll_layer, ["OBJECTID", "SHAPE@"]) as search_cursor:
            for ll_oid, geom in search_cursor:
                if geom is None:
                    continue
                if geom.firstPoint is not None:
                    insert_cursor.insertRow([arcpy.PointGeometry(geom.firstPoint, spatial_ref), ll_oid, end_id])
                    end_id += 1
                if geom.lastPoint is not None:
                    insert_cursor.insertRow([arcpy.PointGeometry(geom.lastPoint, spatial_ref), ll_oid, end_id])
                    end_id += 1

    return output_path


def _get_count(arcpy, layer_or_table):
    return int(arcpy.management.GetCount(layer_or_table)[0])


def _select_lls_contacting_mls(arcpy, ml_oid_list, ml_source, ll_source, seed_ml_layer=None, log_fn=None):
    if not ml_oid_list and seed_ml_layer is None:
        return []

    ml_layer = None
    if ml_oid_list:
        query = f"OBJECTID IN ({','.join(map(str, ml_oid_list))})"
        ml_layer = _replace_temp_layer(arcpy, "_TmpML", ml_source, query)
    ll_layer = _replace_temp_layer(arcpy, "_TmpLL", ll_source)
    candidate_ll_layer = None
    endpoint_fc = None
    endpoint_layer = None
    try:
        selection_made = False
        if ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                ll_layer,
                "WITHIN_A_DISTANCE",
                ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="NEW_SELECTION",
            )
            selection_made = True
        if seed_ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                ll_layer,
                "WITHIN_A_DISTANCE",
                seed_ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="ADD_TO_SELECTION" if selection_made else "NEW_SELECTION",
            )
            selection_made = True

        if not selection_made:
            return []

        candidate_ll_layer = _replace_temp_layer(arcpy, "_TmpLLCandidates", ll_layer)
        if log_fn:
            log_fn(f"Candidate LL count before endpoint filter: {_get_count(arcpy, candidate_ll_layer)}")

        endpoint_fc = _build_ll_endpoint_table(arcpy, candidate_ll_layer)
        endpoint_layer = _replace_temp_layer(arcpy, "_TmpLLEndpointLayer", endpoint_fc)
        arcpy.management.SelectLayerByAttribute(endpoint_layer, "CLEAR_SELECTION")

        endpoint_selection_made = False
        if ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                endpoint_layer,
                "WITHIN_A_DISTANCE",
                ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="NEW_SELECTION",
            )
            endpoint_selection_made = True
        if seed_ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                endpoint_layer,
                "WITHIN_A_DISTANCE",
                seed_ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="ADD_TO_SELECTION" if endpoint_selection_made else "NEW_SELECTION",
            )

        if log_fn:
            log_fn(f"LL endpoint hits after {LL_SEARCH_DISTANCE} filter: {_get_count(arcpy, endpoint_layer)}")

        ll_oids = sorted({row[0] for row in arcpy.da.SearchCursor(endpoint_layer, ["LL_OID"])})
        return ll_oids
    finally:
        if ml_layer is not None and arcpy.Exists(ml_layer):
            arcpy.management.Delete(ml_layer)
        if arcpy.Exists(ll_layer):
            arcpy.management.Delete(ll_layer)
        if candidate_ll_layer is not None and arcpy.Exists(candidate_ll_layer):
            arcpy.management.Delete(candidate_ll_layer)
        if endpoint_layer is not None and arcpy.Exists(endpoint_layer):
            arcpy.management.Delete(endpoint_layer)
        if endpoint_fc is not None and arcpy.Exists(endpoint_fc):
            arcpy.management.Delete(endpoint_fc)




def _copy_endpoint_subset(arcpy, source_layer, endpoint_ids, output_name="_TmpParcelSideEndpoints"):
    output_path = f"in_memory\\{output_name}"
    if arcpy.Exists(output_path):
        arcpy.management.Delete(output_path)

    source_desc = arcpy.Describe(source_layer)
    spatial_ref = source_desc.spatialReference
    arcpy.management.CreateFeatureclass("in_memory", output_name, "POINT", spatial_reference=spatial_ref)

    endpoint_id_set = set(endpoint_ids)
    with arcpy.da.InsertCursor(output_path, ["SHAPE@"]) as insert_cursor:
        with arcpy.da.SearchCursor(source_layer, ["SHAPE@", "END_ID"]) as search_cursor:
            for geom, end_id in search_cursor:
                if end_id in endpoint_id_set:
                    insert_cursor.insertRow([geom])

    return output_path


def _select_parcels_intersecting_lls(
    arcpy,
    ll_oid_list,
    ll_source,
    parcels_source,
    ml_oid_list=None,
    ml_source=None,
    seed_ml_layer=None,
    log_fn=None,
):
    if not ll_oid_list:
        return []
    query = f"OBJECTID IN ({','.join(map(str, ll_oid_list))})"
    ll_layer = _replace_temp_layer(arcpy, "_TmpLL", ll_source, query)
    parcels_layer = _replace_temp_layer(arcpy, "_TmpParcels", parcels_source)
    ml_layer = None
    endpoint_fc = None
    endpoint_layer = None
    parcel_endpoint_fc = None
    parcel_endpoint_layer = None
    try:
        endpoint_fc = _build_ll_endpoint_table(arcpy, ll_layer, "_TmpParcelLLEndpoints")
        endpoint_layer = _replace_temp_layer(arcpy, "_TmpParcelLLEndpointLayer", endpoint_fc)

        endpoint_ids = {row[0] for row in arcpy.da.SearchCursor(endpoint_layer, ["END_ID"])}
        ml_endpoint_ids = set()
        selection_made = False

        if ml_oid_list and ml_source:
            ml_query = f"OBJECTID IN ({','.join(map(str, ml_oid_list))})"
            ml_layer = _replace_temp_layer(arcpy, "_TmpParcelML", ml_source, ml_query)
            arcpy.management.SelectLayerByLocation(
                endpoint_layer,
                "WITHIN_A_DISTANCE",
                ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="NEW_SELECTION",
            )
            selection_made = True

        if seed_ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                endpoint_layer,
                "WITHIN_A_DISTANCE",
                seed_ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="ADD_TO_SELECTION" if selection_made else "NEW_SELECTION",
            )
            selection_made = True

        if selection_made:
            ml_endpoint_ids = {row[0] for row in arcpy.da.SearchCursor(endpoint_layer, ["END_ID"])}

        parcel_endpoint_ids = endpoint_ids - ml_endpoint_ids
        if log_fn:
            log_fn(f"LL parcel-side endpoint count: {len(parcel_endpoint_ids)}")

        if not parcel_endpoint_ids:
            return []

        arcpy.management.SelectLayerByAttribute(endpoint_layer, "CLEAR_SELECTION")
        parcel_endpoint_fc = _copy_endpoint_subset(arcpy, endpoint_layer, parcel_endpoint_ids)
        parcel_endpoint_layer = _replace_temp_layer(
            arcpy,
            "_TmpParcelSideEndpointLayer",
            parcel_endpoint_fc,
        )

        arcpy.management.SelectLayerByLocation(
            parcels_layer, "INTERSECT", parcel_endpoint_layer, selection_type="NEW_SELECTION"
        )
        return [row[0] for row in arcpy.da.SearchCursor(parcels_layer, ["OBJECTID"])]
    finally:
        if ml_layer is not None and arcpy.Exists(ml_layer):
            arcpy.management.Delete(ml_layer)
        if arcpy.Exists(ll_layer):
            arcpy.management.Delete(ll_layer)
        if arcpy.Exists(parcels_layer):
            arcpy.management.Delete(parcels_layer)
        if endpoint_layer is not None and arcpy.Exists(endpoint_layer):
            arcpy.management.Delete(endpoint_layer)
        if endpoint_fc is not None and arcpy.Exists(endpoint_fc):
            arcpy.management.Delete(endpoint_fc)
        if parcel_endpoint_layer is not None and arcpy.Exists(parcel_endpoint_layer):
            arcpy.management.Delete(parcel_endpoint_layer)
        if parcel_endpoint_fc is not None and arcpy.Exists(parcel_endpoint_fc):
            arcpy.management.Delete(parcel_endpoint_fc)


def run(log_fn):
    log_fn(f"Phase 4 interpreter: {sys.executable}")
    arcpy = _import_arcpy()

    config = load_config()
    ml_source      = config["p4_ml_source_path"]
    ml_dest        = config["p4_ml_dest_path"]
    ll_source      = config["p4_ll_source_path"]
    ll_dest        = config["p4_ll_dest_path"]
    parcels_source = config["p4_parcels_source_path"]
    parcels_dest   = config["p4_parcels_dest_path"]

    product_info = arcpy.ProductInfo()
    if product_info in {"NotInitialized", "Unavailable", "Engine", "ArcServer"}:
        raise RuntimeError(f"ArcGIS Pro license not available: {product_info}")
    log_fn(f"ArcGIS product license: {product_info}")

    _preflight_phase4(arcpy, config, log_fn)

    pm_mainlines_path = config["feature_class_path"]
    new_assetnums = {}
    with arcpy.da.SearchCursor(pm_mainlines_path, [config["unique_id_field"], "assetnum", "new_flag"]) as cursor:
        for row in cursor:
            if row[2] == 1 and row[1] is not None:
                new_assetnums[str(row[0])] = str(row[1])

    log_fn(f"Found {len(new_assetnums)} new HFMLs with new_flag=1")

    if not new_assetnums:
        log_fn("No new flagged HFMLs — Phase 4 complete")
        _write_phase4_map_manifest([], log_fn)
        return

    assetnum_to_geom = {}
    assetnum_set = set(new_assetnums.values())
    with arcpy.da.SearchCursor(ml_source, ["MXASSETNUM", "SHAPE@"]) as cursor:
        for row in cursor:
            if str(row[0]) in assetnum_set and row[1] is not None:
                assetnum_to_geom[str(row[0])] = row[1]

    log_fn(f"Matched {len(assetnum_to_geom)}/{len(assetnum_set)} assetnums to geometry in ML source")

    new_hfmls = []
    for pmnum, assetnum in new_assetnums.items():
        if assetnum in assetnum_to_geom:
            new_hfmls.append({"pmnum": pmnum, "geom": assetnum_to_geom[assetnum]})
        else:
            log_fn(f"  pmnum {pmnum} assetnum {assetnum} has no geometry in ML source — skipping")

    if not new_hfmls:
        log_fn("No new HFMLs resolved to geometry — Phase 4 complete")
        _write_phase4_map_manifest([], log_fn)
        return

    blacklist = _build_blacklist(arcpy, ml_source)

    hfml_records = []
    for i, hfml in enumerate(new_hfmls, 1):
        pmnum = hfml["pmnum"]
        start_geom = hfml["geom"]
        log_fn(f"--- HFML {i}/{len(new_hfmls)}: pmnum {pmnum} ---")

        network, geoms = _build_network_from_source(arcpy, ml_source, start_geom, log_fn)

        ml_oids = _trace_upstream(start_geom, network, geoms, blacklist, log_fn)
        log_fn(f"Traced ML count: {len(ml_oids)}")
        log_fn(f"Traced ML OID preview: {_preview_oids(ml_oids)}")
        _append_by_oids(arcpy, ml_oids, ml_source, ml_dest)
        log_fn(f"Appended {len(ml_oids)} upstream ML segments")

        seed_ml_layer = _make_seed_ml_layer_from_geom(arcpy, ml_source, start_geom)
        try:
            ll_oids = _select_lls_contacting_mls(
                arcpy, ml_oids, ml_source, ll_source,
                seed_ml_layer=seed_ml_layer, log_fn=log_fn,
            )
            log_fn(f"Selected LL count: {len(ll_oids)}")
            log_fn(f"Selected LL OID preview: {_preview_oids(ll_oids)}")
            _append_by_oids(arcpy, ll_oids, ll_source, ll_dest)
            log_fn(f"Appended {len(ll_oids)} LLs")

            parcel_oids = _select_parcels_intersecting_lls(
                arcpy, ll_oids, ll_source, parcels_source,
                ml_oid_list=ml_oids, ml_source=ml_source,
                seed_ml_layer=seed_ml_layer, log_fn=log_fn,
            )
            log_fn(f"Selected parcel count: {len(parcel_oids)}")
            log_fn(f"Selected parcel OID preview: {_preview_oids(parcel_oids)}")
            _append_by_oids(arcpy, parcel_oids, parcels_source, parcels_dest)
            log_fn(f"Appended {len(parcel_oids)} parcels")

            hfml_records.append({
                "pmnum": pmnum,
                "ml_oids": list(ml_oids),
                "ll_oids": list(ll_oids),
                "parcel_oids": list(parcel_oids),
                "counts": {"ml": len(ml_oids), "ll": len(ll_oids), "parcels": len(parcel_oids)},
            })
        finally:
            if arcpy.Exists(seed_ml_layer):
                arcpy.management.Delete(seed_ml_layer)

        log_fn(f"HFML {pmnum} complete")

    _write_phase4_map_manifest(hfml_records, log_fn)
    log_fn(f"Phase 4 complete — {len(hfml_records)} HFMLs processed")
