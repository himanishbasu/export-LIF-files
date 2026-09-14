"""
lif_to_tif.py
Converts all image series in a Leica .lif file to ImageJ-compatible
hyperstack TIFFs, saved in a folder named after the .lif file.

Dependencies:
    pip install readlif numpy tifffile
    pip install PySimpleGUI
"""
import PySimpleGUI as sg
import sys
import os
import re
import numpy as np
import tifffile
import tkinter as tk
from tkinter import messagebox
from readlif.reader import LifFile




# ── Select .lif file via GUI ───────────────────────────────────────────────────
LIF_PATH = sg.popup_get_file("Select a file")
if not LIF_PATH:
    print("No file selected. Exiting.")
    sys.exit()

# ── Open the .lif file ─────────────────────────────────────────────────────────
lif = LifFile(LIF_PATH)
print(f"Opened: {LIF_PATH}")
print(f"Total images in file: {lif.num_images}\n")

# ── Load all image series ──────────────────────────────────────────────────────
# "index" stores the original .lif series number and is preserved through all
# filtering steps so every downstream reference stays unambiguous.
images = []
for idx, img in enumerate(lif.get_iter_image()):
    images.append({
        "index":     idx,
        "name":      img.name,
        "lif_image": img,
        "dims":      img.dims,
        "channels":  img.channels,
        "bit_depth": img.bit_depth,
        "scale_um":  img.scale,
    })

# ── Print summary ──────────────────────────────────────────────────────────────
print(f"{'#':<4} {'Name':<40} {'X':>6} {'Y':>6} {'Z':>4} {'T':>4} {'CH':>4} {'Bits'}")
print("-" * 80)
for img in images:
    d    = img["dims"]
    bits = "/".join(str(b) for b in img["bit_depth"])
    print(f"{img['index']:<4} {img['name']:<40} {d.x:>6} {d.y:>6} {d.z:>4} {d.t:>4} "
          f"{img['channels']:>4} {bits}")

# ── Keyword filter GUI ─────────────────────────────────────────────────────────
def getKeywordFilter():
    """Prompt for a keyword and an include/exclude mode. Returns (keyword, exclude_bool)."""
    result = {"keyword": None, "exclude": False}

    root = tk.Tk()
    root.title("Image Keyword Filter")
    root.geometry("400x190")

    tk.Label(root, text="Enter keyword to filter image names (leave blank for all):").pack(pady=10)
    entry = tk.Entry(root, width=40)
    entry.pack(pady=5)

    mode_var = tk.StringVar(master=root, value="include")
    mode_frame = tk.Frame(root)
    mode_frame.pack(pady=8)
    tk.Radiobutton(mode_frame, text="Include matches", variable=mode_var, value="include").pack(side="left", padx=10)
    tk.Radiobutton(mode_frame, text="Exclude matches", variable=mode_var, value="exclude").pack(side="left", padx=10)

    def apply():
        result["keyword"] = entry.get().strip().lower()
        result["exclude"] = (mode_var.get() == "exclude")
        root.quit()       # exits mainloop cleanly

    tk.Button(root, text="Confirm Keyword", command=apply).pack(pady=10)
    root.bind("<Return>", lambda e: apply())   # Enter key shortcut

    root.mainloop()
    root.destroy()        # safe to destroy after mainloop exits
    return result["keyword"], result["exclude"]


def keywordFilter(images):
    """Filter images by keyword; returns filtered subset (dicts with original indices).
    exclude=False keeps images whose name contains the keyword (default).
    exclude=True drops images whose name contains the keyword."""
    kw_text, exclude = getKeywordFilter()

    if not kw_text:
        return images   # no keyword → pass all through unchanged

    if exclude:
        filtered = [img for img in images if kw_text not in img["name"].lower()]
    else:
        filtered = [img for img in images if kw_text in img["name"].lower()]

    if not filtered:
        msg = (f"No images remain after excluding '{kw_text}'. Showing all images instead."
               if exclude else
               f"No images contain '{kw_text}'. Showing all images instead.")
        messagebox.showwarning("No Matches", msg)
        return images

    return filtered

filtered_images = keywordFilter(images)
print(f"\nImages after keyword filter: {[img['index'] for img in filtered_images]}")

# ── Image selector GUI ─────────────────────────────────────────────────────────
# Receives filtered_images (any subset of images[]).
# Uses img["index"] throughout — so labels and return values always refer to
# the original .lif series number, not the position in the filtered list.
def selectImages(images):
    """Show a checklist of image series; returns list of selected original indices."""
    selected_indices = []

    root = tk.Tk()
    root.title("Select images to export")
    root.resizable(True, True)
    root.geometry("720x720")

    # ── Scrollable checklist ──────────────────────────────────────────────────
    container = tk.Frame(root)
    container.pack(fill="both", expand=True, padx=10, pady=(10, 4))

    canvas    = tk.Canvas(container, highlightthickness=0)
    scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
    inner     = tk.Frame(canvas)

    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    # ── Header ────────────────────────────────────────────────────────────────
    header = (f"  {'#':<4} {'Name':<40} {'X':>6} {'Y':>6} {'Z':>4} {'T':>4} {'CH':>4} {'Bits'}")
    tk.Label(inner, text=header, font=("Courier", 10, "bold"),
             anchor="w").pack(fill="x", pady=(2, 0))
    tk.Frame(inner, height=1, bg="gray").pack(fill="x", pady=(2, 2))

    row_height = 22
    visible    = min(len(images), 20)
    canvas.configure(height=(visible + 2) * row_height)

    # Build one row per image — use img["index"] for the # column
    check_vars = []
    for img in images:
        d    = img["dims"]
        bits = "/".join(str(b) for b in img["bit_depth"])
        label = (f"{img['index']:<4} {img['name']:<40} {d.x:>6} {d.y:>6} {d.z:>4} {d.t:>4} "
                 f"{img['channels']:>4} {bits}")
        var = tk.BooleanVar(master=root, value=True)   # ← master=root added
        check_vars.append(var)
        tk.Checkbutton(inner, text=label, variable=var,
                       font=("Courier", 10), anchor="w").pack(fill="x")

    # ── Select all / Deselect all ─────────────────────────────────────────────
    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(4, 0))

    tk.Button(btn_frame, text="Select all",
              command=lambda: [v.set(True)  for v in check_vars]).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Deselect all",
              command=lambda: [v.set(False) for v in check_vars]).pack(side="left", padx=6)

    # ── Confirm — return original .lif indices, not list positions ────────────
    def confirm():
        chosen = [img["index"] for img, v in zip(images, check_vars) if v.get()]
        if not chosen:
            messagebox.showwarning("Nothing selected", "Please select at least one image.")
            return
        nonlocal selected_indices
        selected_indices = chosen
        root.quit()        # ← exits mainloop cleanly

    tk.Button(root, text="Export selected", width=16, command=confirm).pack(pady=(6, 12))

    root.mainloop()
    root.destroy()         # ← destroy after mainloop exits
    return selected_indices

selected_indices = selectImages(filtered_images)
print(f"Images selected for export: {selected_indices}")

# ── Export selected series as ImageJ hyperstacks ───────────────────────────────
def sanitize_name(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()

lif_stem   = os.path.splitext(os.path.basename(LIF_PATH))[0]
lif_dir    = os.path.dirname(LIF_PATH)
export_dir = os.path.join(lif_dir, lif_stem)
os.makedirs(export_dir, exist_ok=True)
print(f"Exporting to: {export_dir}\n")

for i in selected_indices:
    img     = images[i]
    lif_img = img["lif_image"]
    d       = img["dims"]
    n_c     = img["channels"]
    bd      = img["bit_depth"]
    scale   = img["scale_um"]

    n_t = max(d.t, 1)
    n_z = max(d.z, 1)
    n_y = d.y
    n_x = d.x

    dtype = np.uint8 if bd[0] <= 8 else np.uint16

    # Build (T, Z, C, Y, X) array frame by frame
    stack = np.zeros((n_t, n_z, n_c, n_y, n_x), dtype=dtype)
    for t in range(n_t):
        for z in range(n_z):
            for c in range(n_c):
                stack[t, z, c] = np.array(lif_img.get_frame(z=z, t=t, c=c), dtype=dtype)

    # ── Per-channel display range from brightest slice ─────────────────────────
    # For each channel, find the (t, z) frame with the highest max pixel value.
    # That frame's max becomes the ImageJ display max; min is fixed at 0.
    ranges = []
    for c in range(n_c):
        channel_data = stack[:, :, c, :, :]            # shape (T, Z, Y, X)
        slice_maxes  = channel_data.max(axis=(-2, -1)) # max per frame → shape (T, Z)
        best_t, best_z = np.unravel_index(slice_maxes.argmax(), slice_maxes.shape)
        display_max  = float(slice_maxes[best_t, best_z])
        ranges.extend([0.0, display_max])
        print(f"         ch{c}: brightest slice t={best_t} z={best_z}  →  display max={display_max:.0f}")

    # Pixel calibration (px/µm → µm/px)
    um_per_px_x = (1.0 / scale[0]) if (scale and scale[0]) else 1.0
    um_per_px_y = (1.0 / scale[1]) if (scale and scale[1]) else 1.0
    um_per_px_z = (1.0 / scale[2]) if (scale and len(scale) > 2 and scale[2]) else 1.0

    out_path  = os.path.join(export_dir, sanitize_name(img["name"]) + ".tif")
    base_meta = {"axes": "TZCYX", "spacing": um_per_px_z, "unit": "um"}
    write_kwargs = dict(imagej=True,
                        resolution=(1.0 / um_per_px_x, 1.0 / um_per_px_y))
    try:
        # New tifffile (≥2023): Ranges as flat list [min0, max0, min1, max1, ...]
        tifffile.imwrite(out_path, stack,
                         metadata={**base_meta, "Ranges": ranges},
                         **write_kwargs)
    except TypeError:
        # Older tifffile: Ranges must be a list of [min, max] pairs per channel
        ranges_paired = [[ranges[c * 2], ranges[c * 2 + 1]] for c in range(n_c)]
        tifffile.imwrite(out_path, stack,
                         metadata={**base_meta, "Ranges": ranges_paired},
                         **write_kwargs)
    print(f"  [{i+1}/{len(images)}] {img['name']}")
    print(f"         shape (T,Z,C,Y,X) = {stack.shape}  |  dtype = {dtype.__name__}  "
          f"|  {um_per_px_x:.4f} µm/px xy  |  {um_per_px_z:.4f} µm/px z")

print(f"\nDone. {len(selected_indices)} file(s) written to:\n  {export_dir}")