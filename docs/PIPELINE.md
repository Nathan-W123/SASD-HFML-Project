# Pipeline Execution Plan

## Phase 1: SQL → Excel Import

1. Read SQL connection string from `config.json`
2. Open connection to SQL database using `pyodbc`
3. Execute the predefined SQL query
4. Fetch all results into a `pandas` DataFrame
5. Export DataFrame to a temporary CSV file in a local temp directory
6. Open the shared Excel workbook using `openpyxl`
7. Check if a sheet named `import` already exists — if so, delete it
8. Create a new sheet named `import`
9. Write CSV data into the `import` sheet row by row, preserving headers
10. Save and close the workbook
11. Delete the temporary CSV file
12. Log: "Phase 1 complete — X rows imported"

---

## Phase 2: Excel Manipulation

### 2a. Detect Added and Removed MLs
1. Open the shared Excel workbook
2. Read the existing data sheet (name defined in config) into memory
3. Read the `import` sheet into memory
4. Compare ML ID columns between the two sheets
5. Identify rows in `import` not present in existing → **added MLs**
6. Identify rows in existing not present in `import` → **removed MLs**
7. Write added MLs to a staging list in memory
8. Write removed MLs to a staging list in memory
9. Log counts: "X MLs added, Y MLs removed"

### 2b. Detect Frequency Changes
1. For ML IDs present in both sheets, compare the `Frequency` column values
2. Flag any ML where frequency has changed
3. Store changed ML IDs and their new frequency values in memory
4. Log: "X frequency changes detected"

### 2c. Add New HFML Column
1. In the `import` sheet, add a new column for HFML designation
2. Populate based on business logic defined in config or constants
3. Log: "HFML column added"

### 2d. Rebuild the Working Sheet
1. Create a new sheet in the workbook named `current`
2. Copy all data from `import` sheet into `current`
3. Delete the `import` sheet
4. Delete the original existing data sheet
5. Rename `current` to the original existing sheet name (read from config)
6. Save and close the workbook
7. Log: "Phase 2 complete — workbook rebuilt"

---

## Phase 3: Sync Excel to ArcGIS Attribute Table

### 3a. Convert Excel to GIS Table
1. Check `arcpy` license availability — raise error if not licensed
2. Set the ArcGIS workspace to the `.aprx` project path from config
3. Run `arcpy.conversion.ExcelToTable` using the shared Excel workbook and the rebuilt working sheet
4. Output to a scratch geodatabase or in-memory workspace
5. Log: "Excel converted to GIS table"

### 3b. Upsert into Attribute Table
1. Open the Append tool: `arcpy.management.Append`
2. Set input dataset to the converted Excel table
3. Set target dataset to the ArcGIS feature class attribute table (path in config)
4. Set schema type to `NO_TEST` with explicit field mapping
5. Map Excel `Frequency` column → ArcGIS `Frequency` field
6. Map all other relevant columns by matching field names
7. Set the unique ID field (from config) as the upsert match key
8. Execute — updates frequency for existing IDs, inserts rows for new IDs
9. Log: "Phase 3 complete — attribute table updated"

---

## Phase 4: Append ML, LL, and Parcels for New HFMLs

1. Read the added HFMLs list produced in Phase 2
2. For each new HFML in the list:
   - Recursively trace upstream from the HFML, finding all ML segments that feed into it and all segments that feed into those, until no further upstream segments exist
   - Append all traced upstream ML segments to the target ML dataset
   - Select all LL features that contact the appended ML segments
   - Append those LL features to the target LL dataset
   - Select all parcel features that intersect the appended LL features
   - Append those parcel features to the target parcels dataset
   - Log counts for each step
3. After all HFMLs processed, log: "Phase 4 complete — X new HFMLs processed"

---

## Phase 5: Generate and Export PDF Maps

### 5a. Determine Template Per HFML
1. Read the list of newly appended HFMLs from Phase 4
2. For each HFML:
   - Retrieve the spatial extent of the appended features
   - Calculate the aspect ratio of the bounding box
   - If aspect ratio is wider than tall → select horizontal template
   - If aspect ratio is taller than wide → select vertical template

### 5b. Generate Map and Export
1. For each HFML:
   - Copy the selected template `.aprx` file to a temporary working copy (do not modify originals)
   - Open the temporary `.aprx` using `arcpy.mp.ArcGISProject`
   - Access the map layout in the project
   - Update the data source or definition query on the relevant layer to scope to current HFML features
   - Zoom the map frame extent to the HFML feature bounding box with a defined buffer margin
   - Update any dynamic text elements (title, date, HFML ID) if present in the template
   - Export the layout to PDF using `arcpy.mp.Layout.exportToPDF`
   - Set output path to: `[pdf_output_dir from config]/[HFML_ID].pdf`
   - Close and delete the temporary `.aprx` copy
   - Log: "Exported map for HFML [ID] → [filename].pdf"
2. After all exports complete, log: "Phase 5 complete — X PDFs exported to [output directory]"

---

## Run All
1. Execute Phase 1
2. On success, execute Phase 2
3. On success, execute Phase 3
4. On success, execute Phase 4
5. On success, execute Phase 5
6. If any phase fails, halt immediately and display the error in the log panel — do not proceed to the next phase
7. On full completion, log: "Pipeline complete"
