import streamlit as st
import pandas as pd
import re
from io import StringIO
import os # Kept for consistency, though not actively used in this snippet

# --- Configuration ---
GROUPING_TOLERANCE_FRAMES = 2 # Allow 1-2 frame gaps for events to be considered part of the same shot

# --- Helpers ---
def timecode_to_frames(tc_str, framerate):
    """Converts HH:MM:SS:FF timecode string to absolute 0-indexed frame count."""
    if not tc_str or not isinstance(tc_str, str):
        return None
    try:
        parts = list(map(int, tc_str.split(":")))
        if len(parts) != 4:
            return None
        h, m, s, f = parts
        # Calculate total frames: (total seconds * framerate) + frames in current second
        # This gives a 0-indexed absolute frame number.
        # E.g., 00:00:00:00 @ 24fps -> frame 0
        # E.g., 00:00:00:01 @ 24fps -> frame 1
        # E.g., 00:00:01:00 @ 24fps -> frame 24 (assuming framerate is exactly 24.0)
        return int(round(((h * 3600 + m * 60 + s) * framerate) + f))
    except (ValueError, TypeError):
        # st.warning(f"Could not parse timecode: {tc_str}") # For debugging
        return None

def frames_to_timecode(frames, framerate):
    """Converts absolute 0-indexed frame count to HH:MM:SS:FF timecode string."""
    if frames is None or framerate is None or framerate <= 0:
        return "00:00:00:00"
    if not isinstance(frames, (int, float)) or not isinstance(framerate, (int, float)):
        return "00:00:00:00"

    frames = int(round(frames)) # Ensure discrete frame number

    # Calculate total seconds from the absolute frame count.
    # 'frames' represents the start of a frame (0-indexed).
    total_seconds_float = frames / framerate

    s_total_int = int(total_seconds_float) # Integer part of total seconds

    # Calculate frame part F for the timecode string
    # This is how many full frames into the current *integer* second 'frames' lands.
    frame_component_float = (total_seconds_float - s_total_int) * framerate
    f = int(round(frame_component_float))

    # Handle potential rollover if f equals or exceeds the display framerate
    # (e.g., if framerate is 23.976, display rate is 24)
    tc_display_rate = int(round(framerate))
    if tc_display_rate > 0:
        if f >= tc_display_rate:
            s_total_int += f // tc_display_rate
            f %= tc_display_rate
    else: # Should not happen with valid framerates
        f = 0

    h, remainder = divmod(s_total_int, 3600)
    m, s = divmod(remainder, 60)

    return f"{int(h):02}:{int(m):02}:{int(s):02}:{int(f):02}"


@st.cache_data # Caching the parsing result for performance
def parse_edl(_edl_text_content, framerate):
    """
    Parses EDL text to extract VFX shots, grouping events to handle multi-layer clips.
    """
    lines = _edl_text_content.splitlines()
    marker_lines = []
    raw_events = []
    in_marker_block = False

    # --- Phase 1: Extract raw events and marker metadata lines ---
    for line_num, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # Regex for standard CMX3600 EDL events (focus on video cuts)
        # Groups: 1:Evt#, 2:Reel, 3:Track, 4:Trans, 5:SrcIn, 6:SrcOut, 7:RecIn, 8:RecOut
        # Making Reel name more flexible: ([\w\d\s./_-]+?)
        match = re.match(
            r"^\s*(\d+)\s+([\w\d\s./_-]+?)\s+([AVU][AVU\d]*)\s*([CKDWBP])\s+(\d{2}:\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2}:\d{2})",
            line,
        )
        if match:
            track_type = match.group(3)
            transition = match.group(4)
            
            # We are primarily interested in Video ('V') tracks and Cut ('C') transitions for basic event definition.
            # More complex transitions (K, D, W) might need further logic if they define VFX shot boundaries.
            if track_type.startswith("V") and transition == "C":
                rec_in_str = match.group(7)
                rec_out_str = match.group(8)
                rec_in_frames = timecode_to_frames(rec_in_str, framerate)
                rec_out_frames = timecode_to_frames(rec_out_str, framerate)

                if rec_in_frames is not None and rec_out_frames is not None and rec_out_frames > rec_in_frames:
                    raw_events.append({
                        "id": f"event_{line_num}", # Unique ID for the event
                        "Clip Name": match.group(2).strip(), # Reel/Clip Name
                        "Source TC IN": match.group(5),
                        "Source TC OUT": match.group(6),
                        "TC IN": rec_in_str,
                        "TC OUT": rec_out_str,
                        "TC IN frames": rec_in_frames,   # 0-indexed start frame (inclusive)
                        "TC OUT frames": rec_out_frames, # 0-indexed frame *after* the last frame (exclusive)
                    })
                # else:
                    # Optional: Log or warn about invalid/zero-duration events if needed
                    # st.warning(f"Skipping event with invalid TCs: {line}")


        # Marker block detection (common patterns)
        if "* MARKER METADATA START" in line or (not marker_lines and "* Marker Metadata" in line and not in_marker_block):
            in_marker_block = True
            continue # Don't process this line as a marker itself
        elif "* MARKER METADATA END" in line and in_marker_block:
            in_marker_block = False
            continue
        elif in_marker_block and line.startswith("*"):
            marker_lines.append(line)
        elif not line.startswith("*") and in_marker_block and marker_lines: # Handle cases where block ends without explicit END
            in_marker_block = False


    # Sort events by Record TC IN frames. This is CRITICAL for the grouping logic.
    events = sorted(raw_events, key=lambda e: e["TC IN frames"])

    # --- Phase 2: Parse marker information ---
    markers = []
    for marker_line_text in marker_lines:
        if "HH_" not in marker_line_text:  # Project-specific VFX marker key
            continue
        try:
            # Extract timecode from marker line (often at the end or after "at")
            tc_match = re.search(r"(\d{2}:\d{2}:\d{2}:\d{2})", marker_line_text)
            marker_tc_str = tc_match.group(1) if tc_match else "00:00:00:00" # Default if no TC found in marker
            
            marker_tc_abs_frames = timecode_to_frames(marker_tc_str, framerate)
            if marker_tc_abs_frames is None:
                # st.warning(f"Could not parse timecode from marker line: {marker_line_text}")
                continue # Skip if marker TC is invalid

            comment_start_index = marker_line_text.find("HH_")
            comment_full = marker_line_text[comment_start_index:].strip()
            
            # Extract VFX Code and Description
            parts = comment_full.split(" ", 1) # Split only on the first space
            vfx_code = parts[0]
            description = ""
            if len(parts) > 1:
                # Clean up description: remove TC prefixes if they exist
                desc_candidate = parts[1].strip()
                desc_candidate = re.sub(r"^(at\s+\d{2}:\d{2}:\d{2}:\d{2}\s*-\s*|-\s*|TC:\s*\d{2}:\d{2}:\d{2}:\d{2}\s*-\s*)", "", desc_candidate).strip()
                description = desc_candidate

            markers.append({
                "VFX CODE": vfx_code,
                "Marker TC String": marker_tc_str,
                "Marker TC frames": marker_tc_abs_frames, # 0-indexed frame
                "Description": description
            })
        except Exception as e:
            # st.warning(f"Error parsing marker line '{marker_line_text[:50]}...': {e}")
            continue # Skip problematic marker lines

    if not events and _edl_text_content: st.warning("No valid video cut events were parsed from the EDL. Check EDL format and content.")
    if not markers and _edl_text_content: st.warning("No 'HH_' prefixed markers found in the EDL metadata section.")

    # --- Phase 3: Match markers to events and perform iterative grouping ---
    combined_data = []
    for marker in markers:
        marker_tc_abs_frames = marker["Marker TC frames"]
        
        best_match_event = None
        smallest_diff_to_marker = float("inf")
        best_match_event_index = -1

        # Find the initial event best matching the marker's timecode
        for i, event in enumerate(events):
            event_tc_in_abs_frames = event["TC IN frames"]
            
            # Marker should ideally be at or after the event's start, or very close before.
            # We prioritize matches where the marker is within the event or at its start.
            diff = abs(marker_tc_abs_frames - event_tc_in_abs_frames)
            
            if marker_tc_abs_frames >= event_tc_in_abs_frames and marker_tc_abs_frames < event["TC OUT frames"]: # Marker is within this event
                diff = 0 # Perfect match if marker is within
            
            if diff < smallest_diff_to_marker:
                smallest_diff_to_marker = diff
                best_match_event = event
                best_match_event_index = i
            elif diff == smallest_diff_to_marker: # Prefer later event if diff is identical (e.g. marker between two events)
                if best_match_event is None or event_tc_in_abs_frames > best_match_event["TC IN frames"]:
                    best_match_event = event
                    best_match_event_index = i
        
        if best_match_event:
            # Initialize shot boundaries with the best match event
            current_shot_tc_in_frames = best_match_event["TC IN frames"]
            current_shot_tc_out_frames = best_match_event["TC OUT frames"] # Exclusive out
            
            # Store the primary event for Source TC info
            primary_source_event = best_match_event

            # Iteratively group subsequent events
            if best_match_event_index + 1 < len(events):
                for i in range(best_match_event_index + 1, len(events)):
                    next_event = events[i]
                    next_event_tc_in_frames = next_event["TC IN frames"]
                    
                    # Condition for grouping:
                    # Next event must start at or before the current shot's end (plus tolerance).
                    if next_event_tc_in_frames <= (current_shot_tc_out_frames + GROUPING_TOLERANCE_FRAMES):
                        # Extend the shot's out time if the next event goes longer
                        current_shot_tc_out_frames = max(current_shot_tc_out_frames, next_event["TC OUT frames"])
                    else:
                        # Gap is too large, this event is not part of the current VFX shot group
                        break 
            
            # Finalize shot details
            final_record_tc_in_str = frames_to_timecode(current_shot_tc_in_frames, framerate)
            final_record_tc_out_str = frames_to_timecode(current_shot_tc_out_frames, framerate)
            
            # Duration = (exclusive TC OUT frames) - (inclusive TC IN frames)
            duration_frames = current_shot_tc_out_frames - current_shot_tc_in_frames
            
            if duration_frames < 0: # Safety check, should not happen with sorted events and max()
                # st.error(f"VFX Code {marker['VFX CODE']}: Calculated negative duration ({duration_frames}). This indicates a logic error or bad EDL data.")
                duration_frames = 0 

            # Source Timecodes
            final_source_tc_in_str = primary_source_event["Source TC IN"]
            source_tc_in_abs_frames = timecode_to_frames(final_source_tc_in_str, framerate)
            final_source_tc_out_str = "00:00:00:00" # Default/fallback

            if source_tc_in_abs_frames is not None and duration_frames >= 0:
                # Source TC Out (exclusive) = Source TC In (inclusive, frames) + duration_frames
                final_source_tc_out_abs_frames = source_tc_in_abs_frames + duration_frames
                final_source_tc_out_str = frames_to_timecode(final_source_tc_out_abs_frames, framerate)
            
            episode_match = re.search(r"_EP(\d+)_", marker["VFX CODE"]) # Attempt to extract episode number
            episode = episode_match.group(1) if episode_match else marker["VFX CODE"].split("_")[1] if "_" in marker["VFX CODE"] and len(marker["VFX CODE"].split("_")) > 1 else ""


            combined_data.append({
                "VFX CODE": marker["VFX CODE"],
                "EPISODE": episode,
                "TC IN/OUT": f"{final_record_tc_in_str} - {final_record_tc_out_str}",
                "Source TC IN": final_source_tc_in_str,
                "Source TC OUT": final_source_tc_out_str,
                "Duration (frames)": duration_frames,
                "Description": marker["Description"],
            })
    return pd.DataFrame(combined_data)

# --- UI ---
st.set_page_config(layout="wide") # Use wide layout for better table display
st.title("📽️ VFX EDL Comparison Tool")

with st.expander("ℹ️ How to Use This App & Recommended NLE Settings (Click to Expand)", expanded=False):
    st.markdown("""
    ### 🎬 Step-by-Step Instructions

    1.  **Prepare your EDL**: Export an EDL from your Non-Linear Editing (NLE) software (e.g., AVID Media Composer, DaVinci Resolve, Premiere Pro).
    2.  **Upload current EDL**: Use the "Step 1" uploader.
    3.  **(Optional) Upload previous CSV**: If you have a CSV from a prior run of this tool, upload it in "Step 2" to see changes.
    4.  **Select frame rate**: Critically important for accurate calculations.
    5.  **(Optional) Include Description**: Check the box if you want the 'Description' column in the downloaded CSV.
    6.  **Review Data**: The parsed data (or comparison) will appear below.
    7.  **Download CSV**: Click the download button.

    #### 🎛️ Recommended NLE Settings for EDL Export (e.g., AVID)
    The goal is a plain text CMX3600 compatible EDL. Settings may vary slightly by NLE.

    -   **List Format/Type**: `CMX 3600` (or similar standard EDL format).
    -   **Tracks**: Select **only the primary video track** that contains the final picture elements relevant for VFX (e.g., V1 after picture lock or a specific VFX source track). Avoid including audio tracks or multiple video tracks if they don't represent the composite you want to analyze.
    -   **Optimize EDL / Consolidate Events**: ✅ **Checked/Enabled** (if available). This often helps by flattening sequences and simplifying the EDL, which can be beneficial for this tool's grouping logic.
    -   **Handles**: Set to `0` frames. This tool calculates durations based on the In/Out points in the EDL itself; handles are not added by this tool.
    -   **Marker Export**:
        -   ✅ **Export Markers** or **All Markers at End** (or similar option).
        -   Ensure marker comments/notes (which should contain your `"HH_"` prefixed VFX codes) are included in the EDL output. The tool looks for markers in a block typically starting with `* Marker Metadata` or `* MARKER METADATA START`.
    -   **Information to Include (Clip Info)**:
        -   ✅ **Source Clip Name** or **Tape Name** (the tool uses this for the "Clip Name" field in the EDL event parsing).
        -   ✅ **Source File Name** (if different and relevant, though "Source Clip Name" is primary).
    -   **Transitions**: This tool primarily parses `C` (Cut) transitions for defining event boundaries. While it attempts to group sequential `C` events, complex shots built with `D` (Dissolve), `W` (Wipe), or `K` (Key) transitions might not be grouped as a single VFX shot if those transitions themselves define the shot's extent. The grouping logic is based on timecode contiguity of cut events.
    -   **Output**: Plain Text File (`.edl` extension).

    **Comparison View Key**:
    -   Values that have changed from the previous CSV are shown as `New Value (was: Old Value)`.
    -   The "Status" column indicates if a VFX CODE is `NEW`, `DELETED`, `CHANGED`, or `UNMODIFIED`.
    """)

# --- File Upload and Options ---
col1, col2 = st.columns(2)
with col1:
    edl_file = st.file_uploader("Step 1: Upload current EDL (.edl)", type=["edl"], help="Upload the Edit Decision List file.")
with col2:
    csv_file = st.file_uploader("Step 2 (Optional): Upload previous CSV to compare", type=["csv"], help="Upload a CSV previously generated by this tool for comparison.")

frame_rate_options = {
    "23.976 fps (23.98)": 23.976, "24 fps": 24.0, "25 fps (PAL)": 25.0,
    "29.97 fps (NDF)": 29.97, "30 fps": 30.0, "48 fps": 48.0,
    "50 fps (PAL)": 50.0, "59.94 fps (NDF)": 59.94, "60 fps": 60.0
}
sorted_frame_rate_labels = sorted(frame_rate_options.keys(), key=lambda k: frame_rate_options[k])

selected_frame_rate_label = st.selectbox(
    "Select frame rate (CRITICAL for correct calculations!)",
    sorted_frame_rate_labels,
    index=sorted_frame_rate_labels.index("23.976 fps (23.98)") if "23.976 fps (23.98)" in sorted_frame_rate_labels else 0,
    help="Choose the frame rate of your EDL source material."
)
frame_rate = frame_rate_options[selected_frame_rate_label]

include_desc = st.checkbox("Include 'Description' column in CSV export", value=True, help="If checked, the 'Description' from markers will be in the output CSV.")

# --- Processing and Display ---
if edl_file:
    try:
        edl_text_content = edl_file.read().decode("utf-8", errors="replace") # Use 'replace' for problematic characters
        
        # Pass the actual content to the cached function
        current_df = parse_edl(edl_text_content, framerate=frame_rate)

        if not current_df.empty:
            st.success(f"EDL parsed successfully. Found {len(current_df)} VFX shots based on 'HH_' markers.")
            
            # Define columns for display and export
            display_df_cols_ordered = ["VFX CODE", "EPISODE", "TC IN/OUT", "Source TC IN", "Source TC OUT", "Duration (frames)"]
            if include_desc and "Description" in current_df.columns:
                display_df_cols_ordered.append("Description")
            
            # Ensure all columns in display_df_cols_ordered exist in current_df before selection
            final_display_cols = [col for col in display_df_cols_ordered if col in current_df.columns]
            
            display_df = current_df[final_display_cols].copy() # DataFrame for direct display

            if csv_file:
                try:
                    prev_df = pd.read_csv(csv_file)
                    # Ensure VFX CODE is string for merging, handle potential float if all numbers
                    current_df_for_merge = current_df.copy()
                    current_df_for_merge["VFX CODE"] = current_df_for_merge["VFX CODE"].astype(str)
                    prev_df["VFX CODE"] = prev_df["VFX CODE"].astype(str)
                    
                    merged_df = pd.merge(current_df_for_merge, prev_df, on="VFX CODE", suffixes=("", "_old"), how="outer")

                    comparison_results = []
                    for _, row in merged_df.iterrows():
                        comp_row = {"VFX CODE": str(row["VFX CODE"])} # Ensure VFX CODE is always present
                        is_new = pd.isna(row.get(final_display_cols[1] + "_old")) and pd.notna(row.get(final_display_cols[1]))
                        is_deleted = pd.notna(row.get(final_display_cols[1] + "_old")) and pd.isna(row.get(final_display_cols[1]))
                        
                        changed_fields = False
                        for col in final_display_cols: # Iterate using the defined display columns
                            if col == "VFX CODE": continue # Already handled

                            current_val = row.get(col)
                            old_val = row.get(col + "_old")
                            
                            current_val_str = str(current_val) if pd.notna(current_val) else ""
                            old_val_str = str(old_val) if pd.notna(old_val) else ""

                            if is_new:
                                comp_row[col] = current_val_str
                            elif is_deleted:
                                comp_row[col] = f"(DELETED - was: {old_val_str})"
                            elif current_val_str != old_val_str:
                                comp_row[col] = f"{current_val_str} (was: {old_val_str})"
                                changed_fields = True
                            else: # Values are the same
                                comp_row[col] = current_val_str
                        
                        # Determine Status
                        if is_new:
                            comp_row["Status"] = "NEW"
                        elif is_deleted:
                            comp_row["Status"] = "DELETED"
                        elif changed_fields:
                            comp_row["Status"] = "CHANGED"
                        else:
                            comp_row["Status"] = "UNMODIFIED"
                        
                        comparison_results.append(comp_row)
                    
                    comparison_display_df = pd.DataFrame(comparison_results)
                    # Reorder columns for better display: Status after VFX CODE
                    if "Status" in comparison_display_df.columns:
                        status_col_data = comparison_display_df.pop("Status")
                        # Create the final ordered list of columns for the comparison display
                        comp_display_cols_final = ["VFX CODE", "Status"] + [col for col in final_display_cols if col != "VFX CODE"]
                        # Filter out any columns that might not exist (e.g. Description if not included)
                        comp_display_cols_final = [c for c in comp_display_cols_final if c in comparison_display_df.columns or c == "Status"]
                        comparison_display_df.insert(1, "Status", status_col_data)
                        comparison_display_df = comparison_display_df[comp_display_cols_final]


                    st.subheader("Comparison with Previous CSV")
                    st.dataframe(comparison_display_df, use_container_width=True, hide_index=True)
                    display_df = current_df[final_display_cols].copy() # Update display_df for export

                except Exception as e:
                    st.error(f"Error processing previous CSV for comparison: {e}")
                    st.exception(e) # Show full traceback for debugging
                    st.subheader("Parsed Current EDL Data (Comparison Failed)")
                    st.dataframe(display_df, use_container_width=True, hide_index=True)
            else: # No CSV file for comparison
                st.subheader("Parsed Current EDL Data")
                st.dataframe(display_df, use_container_width=True, hide_index=True)

            # --- CSV Export Button ---
            if not display_df.empty : # Ensure there's data to download
                csv_buffer = StringIO()
                # Use final_display_cols to ensure correct columns are exported
                export_df = display_df[final_display_cols].copy()
                export_df.to_csv(csv_buffer, index=False, encoding='utf-8')
                st.download_button(
                    label="📥 Download Processed Data as CSV",
                    data=csv_buffer.getvalue(),
                    file_name="vfx_edl_processed.csv",
                    mime="text/csv",
                    key="download_csv_button"
                )
        elif edl_file: # File was uploaded, but parse_edl returned empty or only warnings were shown
             st.info("EDL was processed, but no 'HH_' markers or no relevant video events were found to generate VFX shot data. Please check your EDL content and marker formatting.")

    except UnicodeDecodeError:
        st.error("Error decoding EDL file. Please ensure it's a plain text file (e.g., UTF-8 or ASCII). If issues persist, try opening in a text editor, re-saving as UTF-8, and re-uploading.")
    except Exception as e:
        st.error(f"An unexpected error occurred during EDL processing: {e}")
        st.exception(e) # Provides full traceback in the Streamlit app for debugging
