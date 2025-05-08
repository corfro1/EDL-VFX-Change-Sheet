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
