import arcpy

# --- change this OBJECTID in "High Freq ML PMs" layer to desired appension
main_id = 113

# Configure Variables
mainline_layer = "High Freq ML PMs"
source_candidates = "MLs not in Upstream High Freq PM"
destination_layer = "Trace Upstream High Freq PM"

try:
    print("Indexing data and building geometry blacklist...")

    # 1. Build the Blacklist of High Freq Line locations
    blacklist_fingerprints = set()
    with arcpy.da.SearchCursor(mainline_layer, ["SHAPE@"]) as cursor:
        for row in cursor:
            geom = row[0]
            fp = (round(geom.firstPoint.X, 3), round(geom.firstPoint.Y, 3),
                  round(geom.lastPoint.X, 3), round(geom.lastPoint.Y, 3))
            blacklist_fingerprints.add(fp)
            blacklist_fingerprints.add((fp[2], fp[3], fp[0], fp[1]))

    # 2. Build the Upstream Network Dictionary
    network_dict = {}
    candidate_geoms = {}

    with arcpy.da.SearchCursor(source_candidates, ["SHAPE@", "OBJECTID"]) as cursor:
        for row in cursor:
            geom = row[0]
            oid = row[1]
            start = (round(geom.firstPoint.X, 3), round(geom.firstPoint.Y, 3))
            end = (round(geom.lastPoint.X, 3), round(geom.lastPoint.Y, 3))
            candidate_geoms[oid] = (start[0], start[1], end[0], end[1])

            if end not in network_dict:
                network_dict[end] = []
            network_dict[end].append((oid, start))

    # 3. Start the Trace
    arcpy.management.SelectLayerByAttribute(mainline_layer, "NEW_SELECTION", f"OBJECTID = {main_id}")
    with arcpy.da.SearchCursor(mainline_layer, ["SHAPE@"]) as cursor:
        start_geom = next(cursor)[0]
        current_search_point = (round(start_geom.firstPoint.X, 3), round(start_geom.firstPoint.Y, 3))

    to_append_ids = []
    stack = [current_search_point]
    visited_ids = set()

    print("Tracing upstream and filtering duplicates...")
    while stack:
        search_pt = stack.pop()

        if search_pt in network_dict:
            for feat_id, upstream_pt in network_dict[search_pt]:
                if feat_id not in visited_ids:
                    feat_fp = candidate_geoms[feat_id]

                    if feat_fp in blacklist_fingerprints:
                        visited_ids.add(feat_id)
                        continue

                    to_append_ids.append(feat_id)
                    visited_ids.add(feat_id)
                    stack.append(upstream_pt)

                    # 4. Final Append
    if to_append_ids:
        print(f"Found {len(to_append_ids)} clean segments. Appending...")
        query = f"OBJECTID IN ({','.join(map(str, to_append_ids))})"
        arcpy.management.SelectLayerByAttribute(source_candidates, "NEW_SELECTION", query)

        # Create the temporary subset
        arcpy.management.MakeFeatureLayer(source_candidates, "FinalSubset")
        arcpy.management.Append("FinalSubset", destination_layer, "NO_TEST")

        # DELETE THE SUBSET AFTER APPEND
        arcpy.management.Delete("FinalSubset")
        print("Appension completed and temporary layer deleted.")
    else:
        print("No upstream segments found.")

except StopIteration:
    print(f"Error: Mainline with ID {main_id} not found.")
except Exception as e:
    print(f"Error: {e}")
finally:
    # Cleanup: Ensure the layer is gone even if an error occurs
    if arcpy.Exists("FinalSubset"):
        arcpy.management.Delete("FinalSubset")