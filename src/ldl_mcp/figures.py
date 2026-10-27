"""Reproducible distribution maps and publication-style figure presets.

The ``nature_like`` preset is a clean, review-ready layout (sans-serif 7 pt,
0.5 pt strokes, Okabe-Ito colour-blind-safe palette, 89/183 mm widths, vector
SVG/PDF with editable text). It does not claim compliance with any journal's
submission rules.

Basemap: bundled Natural Earth 1:50m land + admin-0 boundaries (public
domain) by default; ``basemap="cartopy"`` uses cartopy features (install the
``[maps]`` extra, also needed for non-plate-carrée projections); a GeoJSON path
or ``LDL_MCP_BASEMAP`` overrides the land layer. ``basemap="none"`` renders a
graticule-only draft.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from .distribution import MAPPABLE_DEFAULT
from .distribution import load as load_dataset
from .paths import now_iso, read_json, stable_id, subdir, write_json_atomic

OKABE_ITO = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9", "#F0E442", "#000000"]
BASIS_MARKERS = {"type_specimen": "D", "literature_locality": "o", "georeferenced": "s", "occurrence": "^"}
BASIS_LABELS = {
    "type_specimen": "Type locality (LDL)",
    "literature_locality": "Literature locality",
    "georeferenced": "Georeferenced locality",
    "occurrence": "GBIF occurrence",
}
# Flags that do not hide a point by default; everything else needs include_flagged=True.
SOFT_FLAGS = {"duplicate_point", "integer_degree_precision", "gbif_no_uncertainty"}
THEMES: dict[str, dict[str, Any]] = {
    "nature_like": {
        "font_size": 7,
        "title_size": 8,
        "line_width": 0.5,
        "marker_size": 18,
        "panel_label_size": 8,
        "widths_mm": {"single": 89, "double": 183},
        "land": "#EDEDED",
        "coast": "#9A9A9A",
        "grid": "#D0D0D0",
        "palette": OKABE_ITO,
        "font_family": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
    },
    "plain_report": {
        "font_size": 10,
        "title_size": 12,
        "line_width": 0.8,
        "marker_size": 30,
        "panel_label_size": 11,
        "widths_mm": {"single": 120, "double": 180},
        "land": "#F2EFE9",
        "coast": "#7F7F7F",
        "grid": "#CCCCCC",
        "palette": OKABE_ITO,
        "font_family": ["DejaVu Sans"],
    },
}


def _figures() -> Path:
    return subdir("figures")


def _auto_extent(points: list[tuple[float, float]]) -> list[float]:
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    lon_min, lon_max, lat_min, lat_max = min(lons), max(lons), min(lats), max(lats)
    pad_lon = max(2.0, (lon_max - lon_min) * 0.12)
    pad_lat = max(2.0, (lat_max - lat_min) * 0.12)
    return [
        max(-180.0, lon_min - pad_lon),
        min(180.0, lon_max + pad_lon),
        max(-90.0, lat_min - pad_lat),
        min(90.0, lat_max + pad_lat),
    ]


def _nice_length(km: float) -> float:
    magnitude = 10 ** math.floor(math.log10(max(km, 1e-6)))
    for step in (1, 2, 5, 10):
        if step * magnitude >= km:
            return step * magnitude
    return 10 * magnitude


def _degree_label(value: float, axis: str) -> str:
    hemi = ("E" if value > 0 else "W" if value < 0 else "") if axis == "lon" else ("N" if value > 0 else "S" if value < 0 else "")
    number = f"{abs(value):.0f}" if float(value).is_integer() else f"{abs(value):.1f}"
    return f"{number}°{hemi}"


DATA_DIR = Path(__file__).resolve().parent / "data"
BUNDLED_LAND = DATA_DIR / "ne_50m_land.geojson.gz"
BUNDLED_BORDERS = DATA_DIR / "ne_50m_admin0.geojson.gz"


def _read_geojson(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        import gzip

        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _geojson_polygons(path: Path, extent: list[float] | None = None) -> list[list[tuple[float, float]]]:
    """Outer rings of all (Multi)Polygons, optionally only those whose bbox meets the extent."""
    data = _read_geojson(path)
    features = data.get("features", [data]) if isinstance(data, dict) else []
    rings: list[list[tuple[float, float]]] = []
    for feature in features:
        geometry = feature.get("geometry", feature) if isinstance(feature, dict) else {}
        kind, coords = geometry.get("type"), geometry.get("coordinates", [])
        polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
        for polygon in polygons:
            if not polygon:
                continue
            ring = [(float(p[0]), float(p[1])) for p in polygon[0]]
            if extent:
                xs, ys = [p[0] for p in ring], [p[1] for p in ring]
                if max(xs) < extent[0] or min(xs) > extent[1] or max(ys) < extent[2] or min(ys) > extent[3]:
                    continue
            rings.append(ring)
    return rings


def _basemap(ax: Any, mode: str, theme: dict[str, Any], use_cartopy: bool, extent: list[float]) -> dict[str, Any]:
    """Draw land (and borders). Order for 'auto': cartopy -> LDL_MCP_BASEMAP -> bundled Natural Earth 1:50m."""
    from matplotlib.collections import LineCollection, PolyCollection

    if mode == "none":
        return {"source": "none"}
    if use_cartopy and mode in {"auto", "cartopy"}:
        try:
            import cartopy
            import cartopy.feature as cfeature

            ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor=theme["land"], edgecolor="none", zorder=0)
            ax.add_feature(cfeature.COASTLINE.with_scale("50m"), edgecolor=theme["coast"], linewidth=theme["line_width"] * 0.8, zorder=1)
            ax.add_feature(cfeature.BORDERS.with_scale("50m"), edgecolor=theme["coast"], linewidth=theme["line_width"] * 0.4, linestyle=":", zorder=1)
            return {"source": "cartopy Natural Earth 1:50m land/coastline/borders", "cartopy_version": cartopy.__version__}
        except Exception:
            if mode == "cartopy":
                raise
    if mode not in {"auto", "cartopy", "bundled"}:
        land_path, border_path = Path(mode).expanduser(), None
    elif mode == "auto" and os.getenv("LDL_MCP_BASEMAP"):
        land_path, border_path = Path(os.environ["LDL_MCP_BASEMAP"]).expanduser(), None
    else:
        land_path, border_path = BUNDLED_LAND, BUNDLED_BORDERS
    pad = [extent[0] - 5, extent[1] + 5, extent[2] - 5, extent[3] + 5]
    land = _geojson_polygons(land_path, pad)
    ax.add_collection(PolyCollection(land, facecolors=theme["land"], edgecolors=theme["coast"], linewidths=theme["line_width"] * 0.6, zorder=0))
    info: dict[str, Any] = {
        "source": "Natural Earth 1:50m land + admin-0 boundaries (bundled, public domain)" if land_path == BUNDLED_LAND else f"GeoJSON {land_path.name}",
        "land_sha256_16": hashlib.sha256(land_path.read_bytes()).hexdigest()[:16],
        "polygons": len(land),
    }
    if border_path is not None:
        borders = _geojson_polygons(border_path, pad)
        ax.add_collection(LineCollection(borders, colors=theme["coast"], linewidths=theme["line_width"] * 0.35, linestyles=(0, (2, 1.5)), zorder=0.8))
        info["borders_sha256_16"] = hashlib.sha256(border_path.read_bytes()).hexdigest()[:16]
        info["note"] = "Admin-0 boundaries follow Natural Earth's de facto depiction; check against your target journal's policy for disputed borders."
    return info


def _ticks(lo: float, hi: float) -> list[float]:
    span = hi - lo
    step = next((st for st in (0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60, 90) if span / st <= 6), 90)
    first = math.ceil(lo / step) * step
    values = []
    value = first
    while value <= hi + 1e-9:
        values.append(round(value, 6))
        value += step
    return values


def _free_corners(points: list[tuple[float, float]], extent: list[float]) -> list[str]:
    """Corners ordered from emptiest to busiest, so map furniture never hides a record."""
    boxes = {
        "lower left": (0.0, 0.32, 0.0, 0.22),
        "lower right": (0.62, 1.0, 0.0, 0.22),
        "upper left": (0.0, 0.32, 0.72, 1.0),
        "upper right": (0.80, 1.0, 0.68, 1.0),
    }
    lon_min, lon_max, lat_min, lat_max = extent

    def count(box: tuple[float, float, float, float]) -> int:
        x0, x1, y0, y1 = box
        n = 0
        for lon, lat in points:
            fx = (lon - lon_min) / max(1e-9, lon_max - lon_min)
            fy = (lat - lat_min) / max(1e-9, lat_max - lat_min)
            n += x0 <= fx <= x1 and y0 <= fy <= y1
        return n

    order = ["lower left", "lower right", "upper left", "upper right"]
    return sorted(order, key=lambda name: (count(boxes[name]), order.index(name)))


def _decorate(
    ax: Any,
    extent: list[float],
    theme: dict[str, Any],
    show_scale: bool,
    ticks: bool,
    points: list[tuple[float, float]] | None = None,
) -> dict[str, str]:
    """Graticule, degree ticks, scale bar and north arrow (plate carrée: data coordinates are degrees)."""
    import matplotlib.patheffects as pe

    lon_min, lon_max, lat_min, lat_max = extent
    lon_ticks, lat_ticks = _ticks(lon_min, lon_max), _ticks(lat_min, lat_max)
    for lon in lon_ticks:
        ax.plot([lon, lon], [lat_min, lat_max], color=theme["grid"], linewidth=theme["line_width"] * 0.5, zorder=0.5)
    for lat in lat_ticks:
        ax.plot([lon_min, lon_max], [lat, lat], color=theme["grid"], linewidth=theme["line_width"] * 0.5, zorder=0.5)
    if ticks:
        ax.set_xticks(lon_ticks)
        ax.set_yticks(lat_ticks)
        ax.set_xticklabels([_degree_label(v, "lon") for v in lon_ticks])
        ax.set_yticklabels([_degree_label(v, "lat") for v in lat_ticks])
        ax.tick_params(length=2, width=theme["line_width"], pad=1.5)
    if not show_scale:
        return {}
    halo = [pe.withStroke(linewidth=2, foreground="white")]
    corners = _free_corners(points or [], extent)
    scale_corner = corners[0]
    arrow_corner = next(c for c in corners if c != scale_corner and c.split()[1] != scale_corner.split()[1]) if any(
        c.split()[1] != scale_corner.split()[1] for c in corners[1:]
    ) else corners[1]
    mid_lat = (lat_min + lat_max) / 2
    km_per_deg = 111.32 * max(0.05, math.cos(math.radians(mid_lat)))
    length_km = _nice_length((lon_max - lon_min) * km_per_deg / 6)
    length_deg = length_km / km_per_deg
    fx = 0.04 if scale_corner.endswith("left") else 0.96 - length_deg / (lon_max - lon_min)
    fy = 0.05 if scale_corner.startswith("lower") else 0.88
    x0 = lon_min + (lon_max - lon_min) * fx
    y0 = lat_min + (lat_max - lat_min) * fy
    ax.plot([x0, x0 + length_deg], [y0, y0], color="black", linewidth=theme["line_width"] * 2.4, solid_capstyle="butt", zorder=6, path_effects=[pe.withStroke(linewidth=theme["line_width"] * 2.4 + 1.5, foreground="white")])
    ax.annotate(f"{length_km:g} km", xy=(x0 + length_deg / 2, y0), xytext=(0, 2), textcoords="offset points", ha="center", va="bottom", fontsize=theme["font_size"] - 1, zorder=6, path_effects=halo)
    ax_x = 0.05 if arrow_corner.endswith("left") else 0.95
    top = 0.95 if arrow_corner.startswith("upper") else 0.30
    ax.annotate("", xy=(ax_x, top), xytext=(ax_x, top - 0.15), xycoords="axes fraction", arrowprops={"arrowstyle": "-|>,head_width=0.25,head_length=0.5", "linewidth": theme["line_width"] * 1.2, "color": "black"}, zorder=6)
    ax.text(ax_x, top - 0.17, "N", transform=ax.transAxes, ha="center", va="top", fontsize=theme["font_size"], fontweight="bold", zorder=6, path_effects=halo)
    return {"scale_bar": scale_corner, "north_arrow": arrow_corner, "scale_note": "scale approximate at map centre latitude (plate carrée)"}


def render_map(
    dataset_id: str,
    species: list[str] | None = None,
    coordinate_bases: list[str] | None = None,
    include_flagged: bool = False,
    extent: list[float] | None = None,
    theme_name: str = "nature_like",
    width: str = "single",
    facet: bool = False,
    projection: str = "platecarree",
    basemap: str = "auto",
    formats: list[str] | None = None,
    dpi: int = 600,
    title: str | None = None,
) -> dict[str, Any]:
    import logging

    import matplotlib

    logging.getLogger("fontTools").setLevel(logging.WARNING)
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    if theme_name not in THEMES:
        raise ValueError(f"theme must be one of {sorted(THEMES)}")
    if width not in {"single", "double"}:
        raise ValueError("width must be single or double")
    formats = [f.lower() for f in (formats or ["svg", "pdf", "png"])]
    if not set(formats) <= {"svg", "pdf", "png"}:
        raise ValueError("formats may contain svg, pdf, png")
    if dpi not in {300, 600}:
        raise ValueError("dpi must be 300 or 600")
    bases = list(coordinate_bases or MAPPABLE_DEFAULT)
    if any(b in {"country_summary", "inferred"} for b in bases):
        raise ValueError("country_summary and inferred rows are never drawn as points; export them as a table instead")
    theme = THEMES[theme_name]
    dataset = load_dataset(dataset_id)
    rows = dataset["rows"]
    wanted = {s.casefold() for s in species} if species else None
    plotted, excluded = [], []
    for row in rows:
        if wanted and str(row.get("species") or "").casefold() not in wanted:
            continue
        if row.get("coordinate_basis") not in bases:
            continue
        if row.get("latitude") is None or row.get("longitude") is None:
            excluded.append({"row_id": row["row_id"], "reason": "no coordinates"})
            continue
        flags = [f for f in str(row.get("qc_flags") or "").split(";") if f]
        hard = [f for f in flags if f in {"out_of_range", "zero_zero", "evidence_rejected", "incomplete_coordinates"}]
        if hard or (flags and not include_flagged and any(f not in SOFT_FLAGS for f in flags)):
            excluded.append({"row_id": row["row_id"], "reason": ",".join(flags)})
            continue
        plotted.append(row)
    if not plotted:
        raise ValueError("No mappable rows after filtering; check coordinate_bases, species and QC flags")

    points = [(float(r["longitude"]), float(r["latitude"])) for r in plotted]
    extent = list(extent) if extent else _auto_extent(points)
    species_names = sorted({str(r["species"]) for r in plotted})
    palette = theme["palette"]
    colours = {name: palette[i % len(palette)] for i, name in enumerate(species_names)}

    use_cartopy = False
    crs = None
    if basemap == "cartopy" or projection != "platecarree":
        try:
            import cartopy.crs as ccrs

            use_cartopy = True
            crs = {"platecarree": ccrs.PlateCarree(), "robinson": ccrs.Robinson(), "mercator": ccrs.Mercator()}.get(projection)
            if crs is None:
                raise ValueError("projection must be platecarree, robinson or mercator")
        except ImportError:
            if projection != "platecarree" or basemap == "cartopy":
                raise ValueError("cartopy is required for this projection/basemap: pip install 'lacewing-digital-library-mcp[maps]'")

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": theme["font_family"],
            "font.size": theme["font_size"],
            "axes.linewidth": theme["line_width"],
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )
    panels = species_names if facet and len(species_names) > 1 else [None]
    ncols = 1 if len(panels) == 1 else min(3, len(panels))
    nrows = math.ceil(len(panels) / ncols)
    width_in = theme["widths_mm"][width] / 25.4
    mid_lat = (extent[2] + extent[3]) / 2
    aspect = (extent[3] - extent[2]) / max(1e-6, (extent[1] - extent[0]) * math.cos(math.radians(mid_lat)))
    panel_h = min(width_in / ncols * aspect, 9.0 / nrows)
    fig = plt.figure(figsize=(width_in, panel_h * nrows + 0.55), constrained_layout=True)
    basemap_info: dict[str, Any] = {}
    placement: dict[str, str] = {}
    for index, panel in enumerate(panels):
        kwargs = {"projection": crs} if use_cartopy else {}
        ax = fig.add_subplot(nrows, ncols, index + 1, **kwargs)
        transform = None
        if use_cartopy:
            import cartopy.crs as ccrs

            transform = ccrs.PlateCarree()
            ax.set_extent(extent, crs=transform)
        else:
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
            ax.set_aspect(1 / max(0.05, math.cos(math.radians(mid_lat))))
        basemap_info = _basemap(ax, basemap, theme, use_cartopy, extent)
        if projection == "platecarree":
            panel_points = [(float(r["longitude"]), float(r["latitude"])) for r in plotted if not panel or r["species"] == panel]
            placement = _decorate(ax, extent, theme, show_scale=index == 0, ticks=not use_cartopy, points=panel_points) or placement
        if use_cartopy:
            gl = ax.gridlines(draw_labels=True, linewidth=0, color=theme["grid"])
            gl.top_labels = gl.right_labels = False
            gl.xlabel_style = gl.ylabel_style = {"size": theme["font_size"] - 1}
        for row in plotted:
            if panel and row["species"] != panel:
                continue
            kw = {"transform": transform} if transform is not None else {}
            external = row["coordinate_basis"] == "occurrence"
            ax.scatter(
                float(row["longitude"]),
                float(row["latitude"]),
                s=theme["marker_size"] * (0.45 if external else 1),
                marker=BASIS_MARKERS.get(row["coordinate_basis"], "o"),
                facecolor=colours[row["species"]],
                edgecolor="none" if external else "black",
                alpha=0.55 if external else 1,
                linewidth=theme["line_width"] * 0.6,
                zorder=3 if external else 4,
                **kw,
            )
        if len(panels) > 1:
            ax.set_title(panel, fontsize=theme["font_size"], fontstyle="italic", pad=2)
            ax.text(-0.02, 1.02, chr(ord("a") + index), transform=ax.transAxes, fontsize=theme["panel_label_size"], fontweight="bold", ha="right", va="bottom")
    handles = []
    if len(panels) == 1:
        for name in species_names:
            handles.append(Line2D([], [], linestyle="none", marker="o", markersize=4, markerfacecolor=colours[name], markeredgecolor="black", markeredgewidth=0.3, label=name))
    for basis in sorted({r["coordinate_basis"] for r in plotted}):
        handles.append(Line2D([], [], linestyle="none", marker=BASIS_MARKERS.get(basis, "o"), markersize=4, markerfacecolor="white", markeredgecolor="black", markeredgewidth=0.5, label=BASIS_LABELS.get(basis, basis)))
    legend = fig.legend(handles=handles, loc="outside lower center", ncol=min(4, max(1, len(handles))), frameon=False, fontsize=theme["font_size"] - 0.5, handletextpad=0.3, columnspacing=1.0)
    for text in legend.get_texts():
        if text.get_text() in species_names:
            text.set_fontstyle("italic")
    if title:
        fig.suptitle(title, fontsize=theme["title_size"])

    qc_report = dataset.get("qc", {})
    final = qc_report.get("status") == "pass" and basemap_info.get("source") != "none"
    params = {
        "dataset_id": dataset_id,
        "species": species,
        "coordinate_bases": bases,
        "include_flagged": include_flagged,
        "extent": extent,
        "theme": theme_name,
        "width": width,
        "facet": facet,
        "projection": projection,
        "basemap": basemap,
        "dpi": dpi,
    }
    figure_id = stable_id(f"map-{dataset_id}", params, 8)
    folder = _figures() / figure_id
    folder.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for fmt in formats:
        path = folder / f"map.{fmt}"
        fig.savefig(path, dpi=dpi if fmt == "png" else None, metadata={"Creator": "lacewing-digital-library-mcp"} if fmt != "png" else None)
        outputs[fmt] = str(path)
    plt.close(fig)
    with (folder / "map_data.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["row_id", "species", "latitude", "longitude", "coordinate_basis", "evidence_level", "locality_normalized", "BibObjID", "EDocID", "pdf_page", "source", "qc_flags"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(plotted)
    write_json_atomic(folder / "map_qc.json", {"dataset_qc": qc_report, "plotted": len(plotted), "excluded": excluded})
    counts: dict[str, int] = {}
    for row in plotted:
        counts[row["coordinate_basis"]] = counts.get(row["coordinate_basis"], 0) + 1
    import matplotlib as mpl

    manifest = {
        "figure_id": figure_id,
        "generated_at": now_iso(),
        "status": "final" if final else "draft",
        "draft_reasons": [r for r, bad in (("dataset QC not passed", qc_report.get("status") != "pass"), ("no basemap", basemap_info.get("source") == "none")) if bad],
        "dataset": {"dataset_id": dataset_id, "content_sha256": dataset.get("content_sha256"), "scope_id": dataset.get("scope_id")},
        "parameters": params,
        "basemap": basemap_info,
        "map_furniture": placement,
        "renderer": {"matplotlib": mpl.__version__},
        "counts": {"plotted": len(plotted), "excluded": len(excluded), "by_coordinate_basis": counts, "species": len(species_names)},
        "outputs": outputs,
        "statement": "Points are only records with an explicit geographic basis. Type-specimen localities are not a complete species distribution; no ranges, hulls or niche models are drawn.",
    }
    write_json_atomic(folder / "figure_manifest.json", manifest)
    basis_text = "; ".join(f"{BASIS_LABELS.get(k, k)} (n = {v})" for k, v in sorted(counts.items()))
    caption = (
        f"**Figure.** Recorded localities of {len(species_names)} {'taxon' if len(species_names) == 1 else 'taxa'} "
        f"({', '.join('*' + s + '*' for s in species_names[:6])}{', …' if len(species_names) > 6 else ''}). "
        f"Symbols show coordinate basis: {basis_text}. Type localities are from LDL georeferenced type-specimen records "
        f"and do not represent complete distributions. Basemap: {basemap_info.get('source')}. "
        f"Data: LDL (lacewing.tamu.edu), dataset {dataset_id}, generated {manifest['generated_at'][:10]}."
        + (" GBIF occurrences (small translucent symbols) are an independent comparison layer from the GBIF.org API;"
           " cite a GBIF download DOI for the same query in publications." if counts.get("occurrence") else "")
        + "\n"
    )
    (folder / "caption.md").write_text(caption, encoding="utf-8")
    return {**manifest, "folder": str(folder), "caption": caption}


def validate(figure_id: str) -> dict[str, Any]:
    folder = _figures() / figure_id
    manifest = read_json(folder / "figure_manifest.json", None)
    if not manifest:
        raise FileNotFoundError(f"Unknown figure_id {figure_id!r}")
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    for fmt, path in manifest.get("outputs", {}).items():
        p = Path(path)
        check(f"{fmt} exists", p.exists() and p.stat().st_size > 0, str(p.name))
        if fmt == "svg" and p.exists():
            text = p.read_text(encoding="utf-8", errors="ignore")
            check("svg text is editable (not outlined)", "<text" in text)
        if fmt == "png" and p.exists():
            data = p.read_bytes()
            match = re.search(rb"pHYs(.{4})(.{4})(.)", data)
            if match:
                ppm = int.from_bytes(match.group(1), "big")
                check("png dpi", round(ppm * 0.0254) >= 300, f"{round(ppm * 0.0254)} dpi")
    for name in ("map_data.csv", "map_qc.json", "caption.md"):
        check(f"{name} exists", (folder / name).exists())
    check("dataset QC passed", "dataset QC not passed" not in manifest.get("draft_reasons", []))
    check("basemap present", manifest.get("basemap", {}).get("source") not in (None, "none"))
    check("no inferred ranges drawn", all(b not in {"inferred", "country_summary"} for b in manifest.get("parameters", {}).get("coordinate_bases", [])))
    return {"figure_id": figure_id, "status": manifest.get("status"), "ok": all(c["ok"] for c in checks), "checks": checks}
