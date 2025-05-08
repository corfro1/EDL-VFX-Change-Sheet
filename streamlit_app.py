import streamlit as st
import pandas as pd
import re
from io import StringIO

# --- Helpers ---
def timecode_to_frames(tc_str, framerate):
    try:
        h, m, s, f = map(int, tc_str.split(":"))
        return int(round(((h * 3600 + m * 60 + s) * framerate) + f))
    except:
        return None

def frames_to_timecode(frames, framerate):
    total_seconds, f = divmod(frames, framerate)
    h, remainder = divmod(total_seconds, 3600)
    m, s = divmod(remainder, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02}:{int(f):02}"

@st.cache_data

def parse_edl(edl_text, framerate):
    lines = edl_text.splitlines()
    marker_lines = []
    events = []
    in_marker_block = False

    # --- Extract event lines ---
    for line in lines:
        match = re.match(
            r"^\d+\s+([\w\d_]+)\s+V\s+C\s+(\d{2}:\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2}:\d{2})",
            line,
        )
        if match:
            events.append({
                "Clip Name": match.group(1),
                "Source TC IN": match.group(2),
                "Source TC OUT": match.group(3),
                "TC IN": match.group(4),
                "TC OUT": match.group(5)
            })
        if "* Marker Metadata" in line:
            in_marker_block = True
        elif in_marker_block and line.startswith("*"):
            marker_lines.append(line)

    # --- Extract marker info using regex ---
    markers = []
    for line in marker_lines:
        if "HH_" not in line:
            continue
        try:
            tc_match = re.search(r"(\d{2}:\d{2}:\d{2}:\d{2})", line)
            marker_tc = tc_match.group(1) if tc_match else "00:00:00:00"
            comment_start = line.find("HH_")
            comment = line[comment_start:].strip()
            vfx_code = comment.split(" ")[0]
            description = comment[len(vfx_code):].strip(" -")
            markers.append({
                "VFX CODE": vfx_code,
                "Marker TC": marker_tc,
                "Description": description
            })
        except:
            continue

    # --- Match markers to events ---
    combined = []
    for marker in markers:
        marker_tc_frames = timecode_to_frames(marker["Marker TC"], framerate)
        best_match = None
        smallest_diff = float("inf")

        for event in events:
            event_tc_in_frames = timecode_to_frames(event["TC IN"], framerate)
            if event_tc_in_frames is not None:
                diff = abs(marker_tc_frames - event_tc_in_frames)
                if diff < smallest_diff:
                    smallest_diff = diff
                    best_match = event

        if best_match:
            tc_in = best_match["TC IN"]
            tc_out = best_match["TC OUT"]
            duration = timecode_to_frames(tc_out, framerate) - timecode_to_frames(tc_in, framerate)
            src_in = best_match["Source TC IN"]
            src_out_frames = timecode_to_frames(best_match["Source TC OUT"], framerate) - 1
            src_out = frames_to_timecode(src_out_frames, framerate)

            combined.append({
                "VFX CODE": marker["VFX CODE"],
                "EPISODE": marker["VFX CODE"].split("_")[1],
                "TC IN/OUT": f"{tc_in} - {tc_out}",
                "Source TC IN": src_in,
                "Source TC OUT": src_out,
                "Duration (frames)": duration,
                "Description": marker["Description"]
            })

    return pd.DataFrame(combined)

# --- UI ---
st.title("📽️ VFX EDL Comparison Tool")

with st.expander("ℹ️ How to Use This App (Click to Expand)"):
    st.markdown("""
    ### 🎬 Step-by-Step Instructions

    **What You Need:**
    - `.edl` file exported from **AVID Media Composer** in **File_129 format**
    - Optional: a `.csv` from a previous run of this app for comparison

    **Required EDL Format:**
    - Must include **Marker Metadata** with VFX Codes in the comment like:
      `HH_103_039_020 - Remove shadow`

    **Steps:**
    1. **Upload your current EDL** (Step 1)
    2. (Optional) **Upload your previous CSV** to compare (Step 2)
    3. Select your **frame rate** (defaults to 24 fps)
    4. Choose whether to **include the description** in export
    5. View the parsed table (bold red = changed)
    6. **Download your final CSV**

    **Important Notes:**
    - `Source TC OUT` is adjusted to subtract 1 frame
    - Duration is calculated in frames (based on the selected frame rate)
    - `VFX CODE` is used to match records between current and previous versions
    """)

edl_file = st.file_uploader("Step 1: Upload current EDL (.edl)", type=["edl"])
csv_file = st.file_uploader("Step 2 (Optional): Upload previous CSV to compare", type=["csv"])

frame_rate = st.selectbox("Select frame rate", [23.976, 24, 25, 29.97], index=1)
include_desc = st.checkbox("Include Description in export", value=True)

if edl_file:
    edl_text = edl_file.read().decode("utf-8")
    current_df = parse_edl(edl_text, framerate=frame_rate)
    
    if csv_file:
        prev_df = pd.read_csv(csv_file)
        merged = current_df.merge(prev_df, on="VFX CODE", suffixes=("", "_old"))

        highlight_df = current_df.copy()
        for col in ["TC IN/OUT", "Source TC IN", "Source TC OUT", "Duration (frames)"]:
            old_col = col + "_old"
            if old_col in merged:
                highlight_df[col] = [
                    f"**{v}**" if str(v) != str(o) else v
                    for v, o in zip(merged[col], merged[old_col])
                ]

        st.subheader("Comparison with Previous CSV")
        st.dataframe(highlight_df)
    else:
        st.subheader("Parsed Current EDL Data")
        st.dataframe(current_df)

    # --- CSV Export ---
    export_df = current_df.copy()
    if not include_desc:
        export_df = export_df.drop(columns=["Description"])
    csv_buffer = StringIO()
    export_df.to_csv(csv_buffer, index=False)
    st.download_button("📥 Download CSV", csv_buffer.getvalue(), file_name="parsed_vfx.csv", mime="text/csv")
