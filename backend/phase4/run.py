import os
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config_loader import load_config

BUFFER_DISTANCE = "50000 Feet"
LL_SEARCH_DISTANCE = "3 Feet"
POC_PMNUM = "9406"
PMNUM_FIELD_CANDIDATES = ("pmnum", "PM_Mainlines_pmnum")
ARCPY_RETRY_ATTEMPTS = 3


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


def _resolve_pmnum_field(arcpy, dataset):
    field_names = _get_field_names(arcpy, dataset)
    lowered = {name.lower(): name for name in field_names}
    for candidate in PMNUM_FIELD_CANDIDATES:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    raise RuntimeError(
        f"Could not find a pmnum field in {dataset}. "
        f"Tried: {', '.join(PMNUM_FIELD_CANDIDATES)}. "
        f"Available fields: {field_names}"
    )


def _sql_literal(value):
    return str(value).replace("'", "''")


def _build_pmnum_query(field_name, pmnum):
    return f"{field_name} = '{_sql_literal(pmnum)}'"


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


def _preflight_phase4(arcpy, config, log_fn):
    dataset_labels = [
        ("PoC ML layer", config["p4_ml_layer_path"]),
        ("ML source", config["p4_ml_source_path"]),
        ("ML destination", config["p4_ml_dest_path"]),
        ("LL source", config["p4_ll_source_path"]),
        ("LL destination", config["p4_ll_dest_path"]),
        ("Parcels source", config["p4_parcels_source_path"]),
        ("Parcels destination", config["p4_parcels_dest_path"]),
    ]
    for label, path in dataset_labels:
        _require_dataset(arcpy, path, label, log_fn)

    ml_layer_pm_field = _resolve_pmnum_field(arcpy, config["p4_ml_layer_path"])
    ml_source_fields = _get_field_names(arcpy, config["p4_ml_source_path"])
    if "OBJECTID" not in ml_source_fields:
        raise RuntimeError(
            f"ML source is missing OBJECTID, which Phase 4 tracing requires: {config['p4_ml_source_path']}"
        )

    log_fn(f"Resolved PoC ML id field: {ml_layer_pm_field}")
    log_fn(f"LL source field count: {len(_get_field_names(arcpy, config['p4_ll_source_path']))}")
    log_fn(f"LL destination field count: {len(_get_field_names(arcpy, config['p4_ll_dest_path']))}")
    log_fn(f"Parcels source field count: {len(_get_field_names(arcpy, config['p4_parcels_source_path']))}")
    log_fn(f"Parcels destination field count: {len(_get_field_names(arcpy, config['p4_parcels_dest_path']))}")
    return ml_layer_pm_field


def _build_network(arcpy, ml_source, ml_layer, log_fn):
    log_fn(f"Spatially filtering network within {BUFFER_DISTANCE} of target HFML...")
    _replace_temp_layer(arcpy, "_NetworkSource", ml_source)
    arcpy.management.SelectLayerByLocation(
        "_NetworkSource", "WITHIN_A_DISTANCE", ml_layer,
        search_distance=BUFFER_DISTANCE,
        selection_type="NEW_SELECTION"
    )
    arcpy.management.SelectLayerByAttribute(ml_layer, "CLEAR_SELECTION")

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


def _get_seed_ml_geometry(arcpy, pmnum, ml_layer, pm_field_name):
    arcpy.management.SelectLayerByAttribute(ml_layer, "NEW_SELECTION", _build_pmnum_query(pm_field_name, pmnum))
    with arcpy.da.SearchCursor(ml_layer, ["SHAPE@"]) as cursor:
        try:
            return next(cursor)[0]
        except StopIteration as exc:
            raise RuntimeError(
                f"PoC HFML pmnum {pmnum} was not found in {ml_layer} using field {pm_field_name}"
            ) from exc


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


def _build_ll_endpoint_table(arcpy, ll_layer, output_name="_TmpLLEndpoints"):
    output_path = f"in_memory\\{output_name}"
    if arcpy.Exists(output_path):
        arcpy.management.Delete(output_path)

    ll_desc = arcpy.Describe(ll_layer)
    spatial_ref = ll_desc.spatialReference
    arcpy.management.CreateFeatureclass("in_memory", output_name, "POINT", spatial_reference=spatial_ref)
    arcpy.management.AddField(output_path, "LL_OID", "LONG")

    with arcpy.da.InsertCursor(output_path, ["SHAPE@", "LL_OID"]) as insert_cursor:
        with arcpy.da.SearchCursor(ll_layer, ["OBJECTID", "SHAPE@"]) as search_cursor:
            for ll_oid, geom in search_cursor:
                if geom is None:
                    continue
                if geom.firstPoint is not None:
                    insert_cursor.insertRow([arcpy.PointGeometry(geom.firstPoint, spatial_ref), ll_oid])
                if geom.lastPoint is not None:
                    insert_cursor.insertRow([arcpy.PointGeometry(geom.lastPoint, spatial_ref), ll_oid])

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

        endpoint_layer = _build_ll_endpoint_table(arcpy, candidate_ll_layer)
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


def _make_seed_ml_layer(arcpy, ml_layer_path, pm_field_name, pmnum):
    return _replace_temp_layer(
        arcpy,
        "_SeedHFML",
        ml_layer_path,
        _build_pmnum_query(pm_field_name, pmnum),
    )


def _select_parcels_intersecting_lls(arcpy, ll_oid_list, ll_source, parcels_source):
    if not ll_oid_list:
        return []
    query = f"OBJECTID IN ({','.join(map(str, ll_oid_list))})"
    ll_layer = _replace_temp_layer(arcpy, "_TmpLL", ll_source, query)
    parcels_layer = _replace_temp_layer(arcpy, "_TmpParcels", parcels_source)
    ll_endpoints = "in_memory\\ll_endpoints"
    try:
        if arcpy.Exists(ll_endpoints):
            arcpy.management.Delete(ll_endpoints)

        ll_desc = arcpy.Describe(ll_source)
        spatial_ref = ll_desc.spatialReference
        arcpy.management.CreateFeatureclass("in_memory", "ll_endpoints", "POINT", spatial_reference=spatial_ref)

        with arcpy.da.InsertCursor(ll_endpoints, ["SHAPE@"]) as insert_cursor:
            with arcpy.da.SearchCursor(ll_layer, ["SHAPE@"]) as search_cursor:
                for (geom,) in search_cursor:
                    if geom is None or geom.lastPoint is None:
                        continue
                    endpoint = arcpy.PointGeometry(geom.lastPoint, spatial_ref)
                    insert_cursor.insertRow([endpoint])

        arcpy.management.SelectLayerByLocation(
            parcels_layer, "INTERSECT", ll_endpoints, selection_type="NEW_SELECTION"
        )
        return [row[0] for row in arcpy.da.SearchCursor(parcels_layer, ["OBJECTID"])]
    finally:
        if arcpy.Exists(ll_layer):
            arcpy.management.Delete(ll_layer)
        if arcpy.Exists(parcels_layer):
            arcpy.management.Delete(parcels_layer)
        if arcpy.Exists(ll_endpoints):
            arcpy.management.Delete(ll_endpoints)


def run(log_fn):
    log_fn(f"Phase 4 interpreter: {sys.executable}")
    arcpy = _import_arcpy()

    config = load_config()
    ml_layer_path  = config["p4_ml_layer_path"]
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

    pm_field_name = _preflight_phase4(arcpy, config, log_fn)

    ml_layer = _replace_temp_layer(arcpy, "_HFMLLayer", ml_layer_path)
    seed_query = _build_pmnum_query(pm_field_name, POC_PMNUM)

    log_fn(f"PoC — processing single HFML pmnum {POC_PMNUM}")
    arcpy.management.SelectLayerByAttribute(ml_layer, "NEW_SELECTION", seed_query)

    log_fn("Indexing network and building geometry blacklist...")
    arcpy.management.SelectLayerByAttribute(ml_layer, "CLEAR_SELECTION")
    blacklist      = _build_blacklist(arcpy, ml_layer)
    arcpy.management.SelectLayerByAttribute(ml_layer, "NEW_SELECTION", seed_query)
    start_geom = _get_seed_ml_geometry(arcpy, POC_PMNUM, ml_layer, pm_field_name)
    network, geoms = _build_network(arcpy, ml_source, ml_layer, log_fn)

    ml_oids = _trace_upstream(start_geom, network, geoms, blacklist, log_fn)
    log_fn(f"Traced ML count: {len(ml_oids)}")
    log_fn(f"Traced ML OID preview: {_preview_oids(ml_oids)}")
    _append_by_oids(arcpy, ml_oids, ml_source, ml_dest)
    log_fn(f"Appended {len(ml_oids)} upstream ML segments")

    seed_ml_layer = _make_seed_ml_layer(arcpy, ml_layer_path, pm_field_name, POC_PMNUM)
    try:
        ll_oids = _select_lls_contacting_mls(
            arcpy,
            ml_oids,
            ml_source,
            ll_source,
            seed_ml_layer=seed_ml_layer,
            log_fn=log_fn,
        )
        log_fn(f"Selected LL count: {len(ll_oids)}")
        log_fn(f"Selected LL OID preview: {_preview_oids(ll_oids)}")
        _append_by_oids(arcpy, ll_oids, ll_source, ll_dest)
        log_fn(f"Appended {len(ll_oids)} LLs contacting upstream MLs and the seed HFML")
    finally:
        if arcpy.Exists(seed_ml_layer):
            arcpy.management.Delete(seed_ml_layer)

    parcel_oids = _select_parcels_intersecting_lls(arcpy, ll_oids, ll_source, parcels_source)
    log_fn(f"Selected parcel count: {len(parcel_oids)}")
    log_fn(f"Selected parcel OID preview: {_preview_oids(parcel_oids)}")
    _append_by_oids(arcpy, parcel_oids, parcels_source, parcels_dest)
    log_fn(f"Appended {len(parcel_oids)} parcels intersecting LLs")

    log_fn(f"Phase 4 PoC complete — pmnum {POC_PMNUM} processed")
