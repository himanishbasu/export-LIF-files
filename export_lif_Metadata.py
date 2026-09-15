# -*- coding: utf-8 -*-

"""
Leica LIF / LIFEXT Metadata Extractor
=====================================

Extracts Leica LIF metadata using readlif and the raw Leica XML header.

- Reads metadata only; does NOT load image pixels.
- Extracts every Leica image/series found in the XML header: name,
  dimensions, pixel size, objective/microscope, binning, and per-channel
  info (Leica channel ID, UserDefName, filter, FluoCube, emission,
  exposure, LUTs, active LED wavelength/intensity).
- Image names use the full LAS X folder breadcrumb (e.g.
  "TileScan1_B_2_R1_Lng_SVCC"), not just the leaf series name.
- Groups images with identical metadata (series names differ, metadata
  doesn't) into one entry, and generates a grouped PDF beside the LIF.
"""

# ---- Imports ----

from pathlib import Path
import re
import io
import html
import xml.etree.ElementTree as ET
from collections import OrderedDict

from readlif.reader import LifFile

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether,
)
import PySimpleGUI as sg




# ---- General helpers ----
def getFilePath():

    layout = [
        [sg.Text("Select a LIF file:")],
        [
            sg.Input(key="-FILE-", size=(50, 1)),
            sg.FileBrowse(
                "Browse",
                file_types=(("LIF Files", "*.lif"),)
            )
        ],
        [sg.Button("OK"), sg.Button("Cancel")]
    ]

    window = sg.Window("LIF File Selector", layout)

    while True:
        event, values = window.read()

        if event in (sg.WINDOW_CLOSED, "Cancel"):
            window.close()
            return None

        if event == "OK":
            file_path = values["-FILE-"]

            if file_path:
                window.close()
                return Path(file_path)   # <-- important

            else:
                sg.popup("Please select a LIF file")

    window.close()
  
def clean_text(value):
    """Convert a value to a clean, stripped, single-line string."""
    if value is None:
        return ""
    value = str(value).replace("\x00", "").replace("\r", " ").replace("\n", " ")
    return value.strip()


def xml_escape(value):
    """Escape text for ReportLab Paragraph XML."""
    return html.escape(clean_text(value))


def get_attr(text, attr_name, default=""):
    """Extract an XML-style attribute (e.g. UserDefName="DAPI" -> DAPI)."""
    if not text:
        return default
    pattern = r'\b' + re.escape(attr_name) + r'\s*=\s*"([^"]*)"'
    match = re.search(pattern, text, flags=re.DOTALL)
    return clean_text(match.group(1)) if match else default


def get_float(value, default=None):
    """Safely convert a value to float."""
    try:
        return float(str(value).strip())
    except Exception:
        return default


def get_int(value, default=None):
    """Safely convert a value to integer."""
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def format_number(value, decimals=4):
    """Format numerical values cleanly (drops trailing zeros)."""
    if value is None:
        return ""
    try:
        value = float(value)
        if abs(value - round(value)) < 1e-12:
            return str(int(round(value)))
        return f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def format_wavelength(value):
    """Format wavelength as nm."""
    if value in (None, ""):
        return ""
    return f"{format_number(value, 2)} nm"


def format_exposure(value):
    """Format exposure time (Leica metadata is commonly stored in seconds)."""
    if value in (None, ""):
        return ""
    value = get_float(value)
    return f"{value:.6g} s" if value is not None else ""


def format_pixel_size(value):
    """Format pixel size in micrometers/pixel."""
    if value in (None, ""):
        return ""
    value = get_float(value)
    return f"{value:.6g} \u00b5m/pixel" if value is not None else ""


def format_binning(binning_text, binning_x, binning_y):
    """
    Format the camera binning value. Leica stores this as:
        BinningText="No Binning"
        BinningXValuePrecise="1"  BinningYValuePrecise="1"
    Prefer the explicit X/Y factors (e.g. "2x2") when they show real
    binning, else fall back to Leica's own BinningText label.
    """
    if binning_x and binning_y and (binning_x != "1" or binning_y != "1"):
        return f"{binning_x}x{binning_y}"
    if binning_text:
        return binning_text
    if binning_x and binning_y:
        return f"{binning_x}x{binning_y}"
    return ""


# ---- Image block extraction ----

def build_hierarchical_name(name_stack, separator="_"):
    """
    Join ancestor Element names into one display name, e.g.:
        TileScan 1 / B / 2 / R1_Lng_SVCC -> "TileScan1_B_2_R1_Lng_SVCC"
    The project's own root Element (name_stack[0]) is excluded, since
    it's just the LIF file name and would be redundant on every entry.
    Spaces are stripped from each segment. To concatenate certain
    adjacent levels without a separator (e.g. "B2" instead of "B_2"),
    this is the one place to adjust that.
    """
    segments = [clean_text(n).replace(" ", "") for n in name_stack[1:] if clean_text(n)]
    return separator.join(segments)


def extract_image_blocks(xml_text):
    """
    Walk the Leica XML tree and extract one block per <Image> element,
    with the full hierarchical name built from every ancestor <Element>
    in the LAS X project tree (e.g. "TileScan1_B_2_R1_Lng_SVCC" rather
    than just "R1_Lng_SVCC"). Each <Image>'s own serialized XML becomes
    the "element" text that the regex-based helpers below search.
    """
    blocks = []
    name_stack = []

    for event, elem in ET.iterparse(io.StringIO(xml_text), events=("start", "end")):
        tag = elem.tag

        if event == "start":
            if tag == "Element":
                name_stack.append(elem.attrib.get("Name", ""))
            continue

        # end event
        if tag == "Image":
            hierarchical_name = build_hierarchical_name(name_stack)
            element_text = ET.tostring(elem, encoding="unicode")
            image_match = re.search(r"<Image\b([^>]*)>", element_text, flags=re.DOTALL)

            blocks.append({
                "element": element_text,
                "element_attributes": "",
                "image_attributes": image_match.group(1) if image_match else "",
                "hierarchical_name": hierarchical_name,
            })
            elem.clear()  # free the (large) parsed subtree now that we have its text

        elif tag == "Element":
            if name_stack:
                name_stack.pop()
            elem.clear()

    return blocks


# ---- Image description ----

def extract_image_description(image_block):
    """
    Extract image-level metadata from the <Image> block: name, dims,
    pixel size, and ChannelDescription LUTName (image/channel pseudocolors).
    """
    image_name = (
        get_attr(image_block["image_attributes"], "Name")
        or get_attr(image_block["element_attributes"], "Name")
    )
    image_id = get_attr(image_block["image_attributes"], "UniqueID")

    channel_descriptions = []
    for m in re.finditer(r"<ChannelDescription\b([^>]*)/?>", image_block["element"], flags=re.DOTALL):
        a = m.group(1)
        channel_descriptions.append({
            "LUTName": get_attr(a, "LUTName"),
            "LUT": get_attr(a, "LUT"),
            "Name": get_attr(a, "Name"),
            "Channel": get_attr(a, "Channel"),
        })

    dimensions = []
    for m in re.finditer(r"<DimensionDescription\b([^>]*)/?>", image_block["element"], flags=re.DOTALL):
        a = m.group(1)
        dimensions.append({
            "NumberOfElements": get_int(get_attr(a, "NumberOfElements")),
            "Length": get_float(get_attr(a, "Length")),
            "Origin": get_float(get_attr(a, "Origin")),
            "Unit": get_attr(a, "Unit"),
            "LengthUnit": get_attr(a, "LengthUnit"),
        })

    # Pixel size: Leica Length is commonly in meters -> convert to um/pixel.
    pixel_sizes = [
        (dim["Length"] / dim["NumberOfElements"]) * 1e6
        for dim in dimensions
        if dim["Length"] is not None and dim["NumberOfElements"] not in (None, 0)
    ]
    pixel_size_x = pixel_sizes[0] if pixel_sizes else None
    pixel_size_y = pixel_sizes[1] if len(pixel_sizes) > 1 else None

    return {
        "image_name": image_name,
        "image_id": image_id,
        "channel_descriptions": channel_descriptions,
        "dimensions": dimensions,
        "pixel_size_x_um": pixel_size_x,
        "pixel_size_y_um": pixel_size_y,
    }


# ---- Wide field channel metadata ----

def extract_channel_blocks(image_block):
    """
    Extract Leica WideFieldChannelInfo blocks: Channel, UserDefName,
    FluoCubeName, EmissionWavelength, LUT, FastFilterWheelChannelInfo,
    IndividualCameraInfo, and active ILLEDWavelength/Intensity.
    """
    xml = image_block["element"]
    pattern = re.compile(
        r"<WideFieldChannelInfo\b([^>]*)>(.*?)</WideFieldChannelInfo>",
        flags=re.DOTALL,
    )
    channel_records = []

    for match in pattern.finditer(xml):
        attrs, body = match.group(1), match.group(2)

        if get_attr(attrs, "ThisIsHSAutofocusInstance", "0") == "1":
            continue  # ignore Leica autofocus channel instances

        channel_id = get_attr(attrs, "Channel")

        # Leica channel IDs: 2000 -> "ch 00", 2001 -> "ch 01", etc.
        channel_number = ""
        channel_id_int = get_int(channel_id)
        if channel_id_int is not None:
            channel_number = (
                f"ch {channel_id_int - 2000:02d}"
                if channel_id_int >= 2000
                else str(channel_id_int)
            )

        user_def_name = get_attr(attrs, "UserDefName")
        fluo_cube = get_attr(attrs, "FluoCubeName")
        emission = get_float(get_attr(attrs, "EmissionWavelength"))
        configuration_lut = get_attr(attrs, "LUT")
        fluo_cube_position = get_attr(attrs, "FluoCubePos")
        intensity = get_float(get_attr(attrs, "Intensity"))

        # Fast filter wheel
        filter_name = filter_position = ""
        filter_match = re.search(
            r"<FastFilterWheelChannelInfo\b([^>]*)(?:/>|>.*?</FastFilterWheelChannelInfo>)",
            body, flags=re.DOTALL,
        )
        if filter_match:
            fa = filter_match.group(1)
            filter_name = get_attr(fa, "Name") or get_attr(fa, "FilterName")
            filter_position = get_attr(fa, "Position") or get_attr(fa, "Channel")

        # IndividualCameraInfo (exposure time, camera LUT)
        exposure_time, camera_lut = None, ""
        camera_pattern = re.compile(
            r"<IndividualCameraInfo\b([^>]*)(?:/>|>.*?</IndividualCameraInfo>)",
            flags=re.DOTALL,
        )
        for cm in camera_pattern.finditer(body):
            ca = cm.group(1)
            exp = get_attr(ca, "ExposureTime")
            if exp != "":
                exposure_time = get_float(exp)
            lut = get_attr(ca, "LUT")
            if lut:
                camera_lut = lut

        # LED metadata: Leica stores multiple slots (ILLEDWavelength0,
        # ILLEDActiveState0, ILLEDIntensity0, ...); only the active one is
        # reported. Checked on the opening tag first, then the body (some
        # Leica versions store it there instead).
        led_index = led_wavelength = led_intensity = None
        wavelength_pattern = re.compile(r'\bILEDWavelength(\d+)\s*=\s*"([^"]*)"')

        for source in (attrs, body):
            for led_match in wavelength_pattern.finditer(source):
                index, wavelength_value = led_match.group(1), led_match.group(2)
                if get_attr(source, f"ILEDActiveState{index}", "0") == "1":
                    led_index = get_int(index)
                    led_wavelength = get_float(wavelength_value)
                    intensity_value = get_attr(source, f"ILEDIntensity{index}")
                    if intensity_value == "" and source is attrs:
                        intensity_value = get_attr(body, f"ILEDIntensity{index}")
                    led_intensity = get_float(intensity_value)
                    break
            if led_index is not None:
                break

        channel_records.append({
            "channel_id": channel_id,
            "channel_number": channel_number,
            "user_def_name": user_def_name,
            "filter_name": filter_name,
            "filter_position": filter_position,
            "fluo_cube": fluo_cube,
            "fluo_cube_position": fluo_cube_position,
            "emission_wavelength": emission,
            "exposure_time": exposure_time,
            "camera_lut": camera_lut,
            "configuration_lut": configuration_lut,
            "led_index": led_index,
            "led_wavelength": led_wavelength,
            "led_intensity": led_intensity,
            "intensity": intensity,
        })

    return channel_records


# ---- Attach image LUTs ----

def attach_image_luts(channels, channel_descriptions):
    """Associate image-level ChannelDescription LUTName values with
    the extracted WideFieldChannelInfo records (ordered by channel)."""
    for index, channel in enumerate(channels):
        image_lut = ""
        if index < len(channel_descriptions):
            desc = channel_descriptions[index]
            image_lut = desc.get("LUTName") or desc.get("LUT") or ""
        channel["image_lut"] = image_lut
    return channels


# ---- Objective / camera settings ----

def extract_objective(image_block):
    """
    Extract objective/microscope/binning metadata from the
    ATLCameraSettingDefinition attachment associated with the image.
    All of these fields live on the opening tag's own attributes.
    """
    xml = image_block["element"]
    objective_records = []
    pattern = re.compile(
        r"<ATLCameraSettingDefinition\b([^>]*)>(.*?)</ATLCameraSettingDefinition>",
        flags=re.DOTALL,
    )

    for match in pattern.finditer(xml):
        attrs = match.group(1)

        objective_name = get_attr(attrs, "ObjectiveName")
        magnification = get_float(get_attr(attrs, "Magnification"))
        numerical_aperture = get_float(get_attr(attrs, "NumericalAperture"))
        immersion = get_attr(attrs, "Immersion")
        microscope_model = get_attr(attrs, "MicroscopeModel")
        objective_number = get_attr(attrs, "ObjectiveNumber")
        objective_position = get_attr(attrs, "ObjectivePos")

        # Binning: BinningText="No Binning", Binning{X,Y}ValuePrecise="1"
        camera_binning = format_binning(
            get_attr(attrs, "BinningText"),
            get_attr(attrs, "BinningXValuePrecise"),
            get_attr(attrs, "BinningYValuePrecise"),
        )

        if any([
            objective_name, magnification is not None, numerical_aperture is not None,
            immersion, microscope_model, objective_number, objective_position, camera_binning,
        ]):
            objective_records.append({
                "objective_name": objective_name,
                "magnification": magnification,
                "numerical_aperture": numerical_aperture,
                "immersion": immersion,
                "microscope_model": microscope_model,
                "objective_number": objective_number,
                "objective_position": objective_position,
                "camera_binning": camera_binning,
            })

    if objective_records:
        return objective_records[0]

    return {
        "objective_name": "", "magnification": None, "numerical_aperture": None,
        "immersion": "", "microscope_model": "", "objective_number": "",
        "objective_position": "", "camera_binning": "",
    }


# ---- Process one image ----

def process_image(image_block, image_index):
    """Extract all metadata for one Leica image/series."""
    image_description = extract_image_description(image_block)
    image_name = image_block.get("hierarchical_name", "") or image_description["image_name"]

    channels = extract_channel_blocks(image_block)
    channels = attach_image_luts(channels, image_description["channel_descriptions"])
    objective = extract_objective(image_block)

    return {
        "image_index": image_index,
        "image_name": image_name,
        "image_id": image_description["image_id"],
        "dimensions": image_description["dimensions"],
        "pixel_size_x_um": image_description["pixel_size_x_um"],
        "pixel_size_y_um": image_description["pixel_size_y_um"],
        "objective": objective,
        "channels": channels,
    }


# ---- Metadata signature / grouping ----

def make_metadata_signature(record):
    """
    Hashable metadata fingerprint used for grouping. The image/series
    name is intentionally excluded, so differently-named series with
    identical metadata land in the same group.
    """
    objective = record["objective"]
    objective_signature = (
        objective.get("objective_name", ""),
        objective.get("magnification"),
        objective.get("numerical_aperture"),
        objective.get("immersion", ""),
        objective.get("microscope_model", ""),
        objective.get("objective_number", ""),
        objective.get("objective_position", ""),
        objective.get("camera_binning", ""),
    )

    dimensions_signature = tuple(
        (dim.get("NumberOfElements"), dim.get("Length"), dim.get("Origin"),
         dim.get("Unit", ""), dim.get("LengthUnit", ""))
        for dim in record["dimensions"]
    )

    channel_signature = tuple(
        (ch.get("channel_id", ""), ch.get("channel_number", ""), ch.get("user_def_name", ""),
         ch.get("filter_name", ""), ch.get("filter_position", ""), ch.get("fluo_cube", ""),
         ch.get("fluo_cube_position", ""), ch.get("emission_wavelength"), ch.get("exposure_time"),
         ch.get("camera_lut", ""), ch.get("configuration_lut", ""), ch.get("image_lut", ""),
         ch.get("led_index"), ch.get("led_wavelength"), ch.get("led_intensity"), ch.get("intensity"))
        for ch in record["channels"]
    )

    return (
        record.get("pixel_size_x_um"),
        record.get("pixel_size_y_um"),
        dimensions_signature,
        objective_signature,
        channel_signature,
    )


def group_images_by_metadata(records):
    """Group images/series with identical metadata into an ordered
    list of {"records": [...]}."""
    groups = OrderedDict()
    for record in records:
        signature = make_metadata_signature(record)
        groups.setdefault(signature, {"records": []})["records"].append(record)
    return list(groups.values())


# ---- PDF styles ----

def make_pdf_styles():
    """Create the ReportLab paragraph/table styles used throughout the PDF."""
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        name="ReportTitleCustom", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=18, leading=22, alignment=TA_CENTER, spaceAfter=12))

    styles.add(ParagraphStyle(
        name="GroupHeadingCustom", parent=styles["Heading1"], fontName="Helvetica-Bold",
        fontSize=14, leading=17, spaceBefore=6, spaceAfter=8, keepWithNext=True))

    styles.add(ParagraphStyle(
        name="SectionHeadingCustom", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=10.5, leading=13, spaceBefore=8, spaceAfter=5, keepWithNext=True))

    styles.add(ParagraphStyle(
        name="BodyCustom", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=8.5, leading=11, spaceAfter=2))

    styles.add(ParagraphStyle(
        name="SmallCustom", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=7.5, leading=9.5, spaceAfter=1))

    styles.add(ParagraphStyle(
        name="SeriesCustom", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=7.5, leading=9, leftIndent=10, firstLineIndent=-10, spaceAfter=1))

    styles.add(ParagraphStyle(
        name="TableHeaderCustom", parent=styles["BodyText"], fontName="Helvetica-Bold",
        fontSize=7, leading=8.5))

    styles.add(ParagraphStyle(
        name="TableBodyCustom", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=6.8, leading=8.2))

    return styles


# ---- PDF table helpers ----

def paragraph_cell(text, styles, header=False):
    """Convert table text to a wrapping/paginating ReportLab Paragraph."""
    style = styles["TableHeaderCustom"] if header else styles["TableBodyCustom"]
    return Paragraph(xml_escape(clean_text(text)), style)


def make_table(data, col_widths, styles, repeat_rows=1):
    """Build a ReportLab table that can split across pages (never a
    giant single cell)."""
    processed = [
        [
            cell if isinstance(cell, Paragraph)
            else paragraph_cell(cell, styles, header=(row_index < repeat_rows))
            for cell in row
        ]
        for row_index, row in enumerate(data)
    ]

    table = Table(processed, colWidths=col_widths, repeatRows=repeat_rows,
                   hAlign="LEFT", splitByRow=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def add_page_number(canvas, doc):
    """Add page number to the bottom-right corner."""
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.drawRightString(letter[0] - 0.5 * inch, 0.35 * inch, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


# ---- PDF group sections ----

def add_series_names(story, records, styles):
    """
    Add image/series names as flowing paragraphs (THE FIX for the
    LayoutError) rather than a giant table cell, so ReportLab can wrap
    them naturally across pages.
    """
    story.append(Paragraph("Images / Series in this metadata group", styles["SectionHeadingCustom"]))
    story.append(Paragraph(f"Number of images/series: <b>{len(records)}</b>", styles["BodyCustom"]))
    story.append(Spacer(1, 3))

    for index, record in enumerate(records, start=1):
        name = record.get("image_name") or "(unnamed image/series)"
        story.append(Paragraph(f"{index}. {xml_escape(name)}", styles["SeriesCustom"]))


def add_basic_metadata(story, record, styles):
    """Add image-independent geometry metadata shared by the group."""
    story.append(Paragraph("Image Geometry", styles["SectionHeadingCustom"]))

    rows = [["Property", "Value"]]
    for index, dim in enumerate(record["dimensions"]):
        axis = chr(ord("X") + index)
        rows.append([f"{axis} dimension", format_number(dim.get("NumberOfElements"))])

    rows.append(["Pixel size X", format_pixel_size(record["pixel_size_x_um"])])
    rows.append(["Pixel size Y", format_pixel_size(record["pixel_size_y_um"])])

    story.append(make_table(rows, [2.0 * inch, 5.5 * inch], styles))


def add_objective_metadata(story, record, styles):
    """Add objective/microscope/binning metadata."""
    objective = record["objective"]
    story.append(Paragraph("Objective / Microscope", styles["SectionHeadingCustom"]))

    magnification = objective.get("magnification")
    rows = [
        ["Property", "Value"],
        ["Microscope model", objective.get("microscope_model", "")],
        ["Objective", objective.get("objective_name", "")],
        ["Magnification", f"{format_number(magnification)}x" if magnification is not None else ""],
        ["Numerical aperture", format_number(objective.get("numerical_aperture"))],
        ["Immersion", objective.get("immersion", "")],
        ["Objective number", objective.get("objective_number", "")],
        ["Objective position", objective.get("objective_position", "")],
        ["Binning", objective.get("camera_binning", "")],
    ]

    story.append(make_table(rows, [2.0 * inch, 5.5 * inch], styles))


def add_channel_metadata(story, record, styles):
    """Add the per-channel metadata table."""
    channels = record["channels"]
    story.append(Paragraph("Channel Metadata", styles["SectionHeadingCustom"]))

    if not channels:
        story.append(Paragraph(
            "No WideFieldChannelInfo records were found for this image.",
            styles["BodyCustom"],
        ))
        return

    headers = ["Channel", "Leica ID", "UserDefName", "Filter", "FluoCube", "Ex",
               "LED power", "Em", "Exposure", "Image LUT", "Camera LUT"]
    rows = [headers]

    for ch in channels:
        led_power = ch.get("led_intensity")
        if led_power is None:
            led_power = ch.get("intensity")

        rows.append([
            ch.get("channel_number", ""),
            ch.get("channel_id", ""),
            ch.get("user_def_name", ""),
            ch.get("filter_name", ""),
            ch.get("fluo_cube", ""),
            format_wavelength(ch.get("led_wavelength")),
            format_number(led_power) if led_power is not None else "",
            format_wavelength(ch.get("emission_wavelength")),
            format_exposure(ch.get("exposure_time")),
            ch.get("image_lut", ""),
            ch.get("camera_lut", ""),
        ])

    # Landscape-like widths inside a portrait letter page; table splits by row.
    widths = [w * inch for w in (0.52, 0.55, 0.95, 0.68, 0.60, 0.58, 0.60, 0.58, 0.62, 0.62, 0.62)]

    story.append(make_table(rows, widths, styles, repeat_rows=1))


def add_group_to_pdf(story, group, group_number, styles):
    """
    Add one complete metadata group. The first record supplies the
    shared metadata, since every record in the group shares one signature.
    """
    records = group["records"]
    if not records:
        return

    representative = records[0]

    story.append(Paragraph(f"Metadata Group {group_number}", styles["GroupHeadingCustom"]))
    story.append(Paragraph(
        f"This group contains <b>{len(records)}</b> image/series entries with identical metadata.",
        styles["BodyCustom"],
    ))
    story.append(Spacer(1, 4))

    # Series names flow as paragraphs (not a table cell) so they can span pages.
    add_series_names(story, records, styles)
    story.append(Spacer(1, 8))

    add_basic_metadata(story, representative, styles)
    story.append(Spacer(1, 6))

    add_objective_metadata(story, representative, styles)
    story.append(Spacer(1, 6))

    add_channel_metadata(story, representative, styles)


# ---- Build PDF ----

def build_pdf(records, PDF_PATH, LIF_PATH):
    """Generate the grouped PDF. No initial image table, no group-summary table."""
    groups = group_images_by_metadata(records)
    styles = make_pdf_styles()

    doc = SimpleDocTemplate(
        str(PDF_PATH), pagesize=letter,
        rightMargin=0.5 * inch, leftMargin=0.5 * inch,
        topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        title=f"{LIF_PATH.stem} Leica Metadata Report",
        author="Leica LIF Metadata Extractor",
    )

    story = [
        Paragraph("Leica LIF Metadata Report", styles["ReportTitleCustom"]),
        Paragraph(xml_escape(LIF_PATH.name), styles["BodyCustom"]),
        Spacer(1, 8),
        Paragraph(f"<b>Total images/series:</b> {len(records)}", styles["BodyCustom"]),
        Paragraph(f"<b>Unique metadata groups:</b> {len(groups)}", styles["BodyCustom"]),
        Spacer(1, 12),
    ]

    for group_number, group in enumerate(groups, start=1):
        if group_number > 1:
            story.append(PageBreak())  # each metadata group starts on a new page
        add_group_to_pdf(story, group, group_number, styles)

    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)


# ---- Console report ----

def print_console_report(records):
    """Print a compact console summary."""
    groups = group_images_by_metadata(records)

    print(f"\n{'=' * 80}\nGROUPED METADATA REPORT\n{'=' * 80}")
    print(f"Total images/series: {len(records)}")
    print(f"Unique metadata groups: {len(groups)}")

    for group_number, group in enumerate(groups, start=1):
        records_in_group = group["records"]
        representative = records_in_group[0]

        print(f"\n{'-' * 80}\nGROUP {group_number}")
        print(f"Number of images/series: {len(records_in_group)}")

        print("\nSeries:")
        for record in records_in_group:
            print(f"  - {record.get('image_name') or '(unnamed)'}")

        print("\nPixel size:")
        print(f"  X: {format_pixel_size(representative.get('pixel_size_x_um'))}")
        print(f"  Y: {format_pixel_size(representative.get('pixel_size_y_um'))}")

        objective = representative["objective"]
        print("\nObjective:")
        print(f"  Name: {objective.get('objective_name', '')}")
        print(f"  Magnification: {format_number(objective.get('magnification'))}x")
        print(f"  NA: {format_number(objective.get('numerical_aperture'))}")
        print(f"  Immersion: {objective.get('immersion', '')}")
        print(f"  Microscope: {objective.get('microscope_model', '')}")
        print(f"  Binning: {objective.get('camera_binning', '')}")

        print("\nChannels:")
        for ch in representative["channels"]:
            led_power = ch.get("led_intensity")
            if led_power is None:
                led_power = ch.get("intensity")
            print(
                f"  {ch.get('channel_number', '')} | ID={ch.get('channel_id', '')} | "
                f"{ch.get('user_def_name', '')} | Filter={ch.get('filter_name', '')} | "
                f"Ex={format_wavelength(ch.get('led_wavelength'))} | "
                f"LED power={format_number(led_power)} | "
                f"Em={format_wavelength(ch.get('emission_wavelength'))} | "
                f"Exposure={format_exposure(ch.get('exposure_time'))} | "
                f"Image LUT={ch.get('image_lut', '')} | Camera LUT={ch.get('camera_lut', '')}"
            )


# ---- Main ----

def main():
    # get file path ##
    LIF_PATH = getFilePath()
    PDF_PATH = LIF_PATH.parent / f"{LIF_PATH.stem}_metadata_report.pdf"
    print(f"\n{'=' * 80}\nLEICA LIF METADATA EXTRACTOR\n{'=' * 80}")
    print(f"\nInput LIF:\n{LIF_PATH}")
    print("\nLoading LIF metadata...")

    if not LIF_PATH.exists():
        raise FileNotFoundError(f"LIF file not found:\n{LIF_PATH}")

    # We only use xml_header; no image pixels are loaded.
    lif = LifFile(str(LIF_PATH))
    print("LIF loaded.")
    print("Reading Leica XML metadata...")
    xml_text = lif.xml_header
    print(f"XML metadata length: {len(xml_text):,} characters")

    print("\nExtracting image/series metadata blocks...")
    image_blocks = extract_image_blocks(xml_text)
    print(f"Found {len(image_blocks):,} image/series metadata blocks.")
    if not image_blocks:
        raise RuntimeError("No Leica image metadata blocks were found.")

    records = []
    print("\nExtracting metadata from each image/series...")
    for index, image_block in enumerate(image_blocks, start=1):
        try:
            records.append(process_image(image_block, index))
        except Exception as exc:
            print(f"WARNING: Could not process image block {index}: {exc}")
    print(f"Successfully processed {len(records):,} image/series records.")

    groups = group_images_by_metadata(records)
    print(f"\nGrouped into {len(groups):,} unique metadata groups.")

    print_console_report(records)

    print("\nGenerating grouped PDF...")
    build_pdf(records, PDF_PATH, LIF_PATH)

    print(f"\n{'=' * 80}\nDONE\n{'=' * 80}")
    print(f"\nPDF saved to:\n{PDF_PATH}\n")


if __name__ == "__main__":
    main()
