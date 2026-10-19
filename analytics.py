"""Bounded, deterministic CSV profiling for the analytics workspace."""

import csv
import math
from io import StringIO

import numpy as np
import pandas as pd
from werkzeug.utils import secure_filename

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_CSV_ROWS = 100_000
MAX_CSV_COLUMNS = 100
MAX_PREVIEW_ROWS = 100
MAX_CELL_CHARACTERS = 32_768
MAX_HEADER_CHARACTERS = 200
MAX_EXACT_INTEGER = "9007199254740991"  # Largest integer JavaScript represents exactly.
ALLOWED_CSV_CONTENT_TYPES = {
    "", "application/csv", "application/vnd.ms-excel",
    "application/octet-stream", "text/csv", "text/plain",
}


class CSVValidationError(ValueError):
    """An upload is valid HTTP input but not a usable CSV dataset."""


def _json_safe(value):
    """Convert numpy/pandas scalars without emitting JSON NaN or Infinity."""
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _is_number(value):
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def _needs_exact_text(value):
    """Detect large integer literals before pandas/JSON can round their digits."""
    value = value.strip()
    if value.startswith(("+", "-")):
        value = value[1:]
    if not value or not value.isascii() or not value.isdigit():
        return False
    digits = value.lstrip("0") or "0"
    return len(digits) > len(MAX_EXACT_INTEGER) or (
        len(digits) == len(MAX_EXACT_INTEGER) and digits > MAX_EXACT_INTEGER
    )


def _read_dataframe(file_storage):
    original_name = file_storage.filename or ""
    safe_name = secure_filename(original_name)
    if not safe_name or "\x00" in original_name:
        raise CSVValidationError("Please choose a file with a valid filename.")
    if not safe_name.lower().endswith(".csv"):
        raise CSVValidationError("Only CSV files are supported.")
    content_type = (file_storage.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_CSV_CONTENT_TYPES:
        raise CSVValidationError("The uploaded file must be a CSV file.")
    raw_data = file_storage.stream.read(MAX_UPLOAD_BYTES + 1)
    if not raw_data:
        raise CSVValidationError("The CSV file is empty.")
    if len(raw_data) > MAX_UPLOAD_BYTES:
        raise CSVValidationError("The CSV file is too large. Maximum size is 10 MB.")
    try:
        csv_text = raw_data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CSVValidationError("The CSV must use UTF-8 encoding.") from error
    if not csv_text.strip():
        raise CSVValidationError("The CSV file is empty.")
    if "\x00" in csv_text:
        raise CSVValidationError("The CSV contains invalid null characters.")

    # Validate the actual header before pandas can rename empty/duplicate names.
    header = None
    data_rows = 0
    exact_text_columns = set()
    try:
        for row in csv.reader(StringIO(csv_text), strict=True):
            if not row:  # Match pandas' handling of genuinely blank lines.
                continue
            if header is None:
                header = [cell.strip() for cell in row]
                if not header or any(not cell for cell in header):
                    raise CSVValidationError("The CSV must contain non-empty column headers.")
                if all(_is_number(cell) for cell in header):
                    raise CSVValidationError("The CSV must contain a header row.")
                if len(header) > MAX_CSV_COLUMNS:
                    raise CSVValidationError("The CSV contains too many columns. Maximum is 100.")
                if any(len(cell) > MAX_HEADER_CHARACTERS for cell in header):
                    raise CSVValidationError("Column headers must be 200 characters or fewer.")
                if len(set(header)) != len(header):
                    raise CSVValidationError("The CSV contains duplicate column headers. Give each column a unique name.")
                continue
            if len(row) != len(header):
                raise CSVValidationError("The CSV is malformed: rows have inconsistent columns.")
            if any(len(cell) > MAX_CELL_CHARACTERS for cell in row):
                raise CSVValidationError("The CSV contains a cell that is too long. Maximum is 32,768 characters.")
            for index, cell in enumerate(row):
                if index not in exact_text_columns and _needs_exact_text(cell):
                    exact_text_columns.add(index)
            data_rows += 1
            if data_rows > MAX_CSV_ROWS:
                raise CSVValidationError("The CSV contains too many rows. Maximum is 100,000.")
    except csv.Error as error:
        raise CSVValidationError("The CSV is malformed or contains an oversized field.") from error
    if header is None:
        raise CSVValidationError("The CSV must contain a header row.")
    if not data_rows:
        raise CSVValidationError("The CSV must contain at least one data row.")

    try:
        dataframe = pd.read_csv(
            StringIO(csv_text), header=0, on_bad_lines="error",
            dtype={index: str for index in exact_text_columns},
        )
    except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as error:
        raise CSVValidationError("The CSV is malformed or could not be parsed.") from error
    if dataframe.empty:
        raise CSVValidationError("The CSV must contain at least one data row.")
    dataframe.columns = header
    return safe_name, dataframe, [header[index] for index in sorted(exact_text_columns)]


def _histogram(values):
    """Compute bins over every finite value, with scaling to avoid overflow."""
    if not len(values):
        return {"labels": [], "counts": [], "edges": []}
    scale = max(float(np.max(np.abs(values))), 1.0)
    scaled = values / scale
    if float(scaled.min()) == float(scaled.max()):
        value = _json_safe(values[0])
        return {"labels": [f"{value:g}"], "counts": [int(len(values))], "edges": [value, value]}
    bin_count = min(10, max(2, math.ceil(math.sqrt(len(values)))))
    counts, edges = np.histogram(scaled, bins=bin_count)
    edges = edges * scale
    return {
        "labels": [f"{low:.4g} – {high:.4g}" for low, high in zip(edges[:-1], edges[1:])],
        "counts": [int(count) for count in counts],
        "edges": [_json_safe(edge) for edge in edges],
    }


def analyze_csv(file_storage):
    """Return full-dataset profiles and charts, plus a bounded row preview."""
    safe_name, dataframe, exact_text_columns = _read_dataframe(file_storage)
    duplicates = int(dataframe.duplicated().sum())
    invalid_numeric_values = {}
    for column in dataframe.select_dtypes(include="number").columns:
        invalid = np.isinf(dataframe[column].to_numpy(dtype=float))
        if invalid.any():
            invalid_numeric_values[column] = int(invalid.sum())
            dataframe.loc[invalid, column] = np.nan

    column_types = {}
    numeric_statistics = {}
    categorical_statistics = {}
    numeric_histograms = {}
    outliers = {}
    numeric_columns = []
    for column in dataframe.columns:
        series = dataframe[column]
        if pd.api.types.is_bool_dtype(series):
            detected_type = "boolean"
        elif pd.api.types.is_integer_dtype(series):
            detected_type = "integer"
        elif pd.api.types.is_float_dtype(series):
            detected_type = "number"
        else:
            detected_type = "text"
        column_types[column] = detected_type

        if detected_type in ("integer", "number"):
            numeric_columns.append(column)
            values = series.dropna().to_numpy(dtype=float)
            # Scaling keeps valid extreme values from overflowing the mean or quartiles.
            stats = {name: None for name in ("mean", "std", "min", "25%", "50%", "75%", "max")}
            stats["count"] = int(len(values))
            stats["sum"] = 0
            if len(values):
                scale = max(float(np.max(np.abs(values))), 1.0)
                scaled = values / scale
                with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                    stats.update({
                        "mean": _json_safe(np.mean(scaled) * scale),
                        "std": _json_safe(np.std(scaled, ddof=1) * scale) if len(values) > 1 else None,
                        "min": _json_safe(values.min()), "max": _json_safe(values.max()),
                    })
                    for name, value in zip(("25%", "50%", "75%"), np.quantile(scaled, [0.25, 0.5, 0.75])):
                        stats[name] = _json_safe(value * scale)
                try:
                    stats["sum"] = _json_safe(math.fsum(values))
                except OverflowError:
                    stats["sum"] = None
            numeric_statistics[column] = stats
            numeric_histograms[column] = _histogram(values)
            # IQR is a screening rule; an unusual value is not necessarily an error.
            if len(values) >= 4:
                scale = max(float(np.max(np.abs(values))), 1.0)
                scaled = values / scale
                q1, q3 = np.quantile(scaled, [0.25, 0.75])
                iqr = q3 - q1
                lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                count = int(((scaled < lower) | (scaled > upper)).sum())
                with np.errstate(over="ignore", invalid="ignore"):
                    lower_bound, upper_bound = lower * scale, upper * scale
                outliers[column] = {
                    "count": count, "percentage": round(100 * count / len(values), 2),
                    "lower_bound": _json_safe(lower_bound), "upper_bound": _json_safe(upper_bound),
                    "method": "IQR",
                }
        else:
            counts = series.value_counts(dropna=True)
            top = counts.head(10)
            categorical_statistics[column] = {
                "unique_count": int(series.nunique(dropna=True)),
                "top_values": [
                    {"value": str(value)[:200], "count": int(count),
                     "percentage": round(100 * int(count) / len(dataframe), 2)}
                    for value, count in top.items()
                ],
                "other_count": int(counts.iloc[10:].sum()),
            }

    correlations = []
    # Normalize before Pearson computation to avoid overflow for very large values.
    if len(numeric_columns) > 1:
        numeric_data = dataframe[numeric_columns].astype(float)
        scales = numeric_data.abs().max().clip(lower=1)
        scaled_data = numeric_data.divide(scales, axis="columns")
        matrix = scaled_data.corr(method="pearson", min_periods=3)
        available = numeric_data.notna().astype("int64")
        pair_counts = available.T.dot(available)
        for index, column_a in enumerate(numeric_columns):
            for column_b in numeric_columns[index + 1:]:
                coefficient = _json_safe(matrix.loc[column_a, column_b])
                if coefficient is not None:
                    correlations.append({
                        "column_a": column_a, "column_b": column_b,
                        "coefficient": round(max(-1.0, min(1.0, coefficient)), 6),
                        "pairs": int(pair_counts.loc[column_a, column_b]),
                    })
        correlations.sort(key=lambda item: abs(item["coefficient"]), reverse=True)
        correlations = correlations[:20]

    missing_values = {str(column): int(count) for column, count in dataframe.isna().sum().items()}
    total_missing = sum(missing_values.values())
    missing_percentage = round(100 * total_missing / dataframe.size, 2)
    insights = [f"Analyzed all {len(dataframe):,} rows across {len(dataframe.columns):,} columns."]
    if total_missing:
        most_missing = max(missing_values, key=missing_values.get)
        insights.append(f"{total_missing:,} cells ({missing_percentage:g}%) are missing; '{most_missing}' has the most ({missing_values[most_missing]:,}).")
    else:
        insights.append("No missing values were detected.")
    if duplicates:
        insights.append(f"{duplicates:,} rows repeat an earlier row. Check whether these duplicates are expected before removing them.")
    if invalid_numeric_values:
        insights.append(f"{sum(invalid_numeric_values.values()):,} non-finite numeric values were treated as missing.")
    if exact_text_columns:
        insights.append(f"{len(exact_text_columns):,} columns contain integers beyond the exact browser arithmetic range (±9,007,199,254,740,991). They were preserved as text to keep every digit; numeric charts exclude them.")
    unusual = sorted(outliers.items(), key=lambda item: item[1]["count"], reverse=True)
    if unusual and unusual[0][1]["count"]:
        name, profile = unusual[0]
        insights.append(f"'{name}' has {profile['count']:,} potential outliers using the 1.5 × IQR rule. Review them in context; this does not establish an error.")
    if correlations and abs(correlations[0]["coefficient"]) >= 0.7:
        strongest = correlations[0]
        insights.append(f"'{strongest['column_a']}' and '{strongest['column_b']}' have a strong linear association (Pearson r = {strongest['coefficient']:.3f}, {strongest['pairs']:,} paired rows). Correlation does not establish causation.")
    constants = [column for column in dataframe.columns if dataframe[column].nunique(dropna=True) == 1]
    if constants:
        insights.append(f"{len(constants):,} columns have only one distinct non-missing value and may add little analytical information.")
    if not numeric_columns:
        insights.append("No numeric columns were detected. Review category frequencies or clean numeric formatting before quantitative analysis.")

    return {
        "filename": safe_name, "rows": len(dataframe), "columns": int(dataframe.shape[1]),
        "column_names": [str(column) for column in dataframe.columns],
        "column_types": column_types, "missing_values": missing_values,
        "total_missing_values": total_missing, "missing_percentage": missing_percentage,
        "duplicate_rows": duplicates, "invalid_numeric_values": invalid_numeric_values,
        "precision_preserved_columns": exact_text_columns,
        "numeric_statistics": numeric_statistics, "categorical_statistics": categorical_statistics,
        "numeric_histograms": numeric_histograms, "correlations": correlations,
        "outliers": outliers, "insights": insights,
        "preview": [
            {column: _json_safe(value) for column, value in row.items()}
            for row in dataframe.head(MAX_PREVIEW_ROWS).to_dict(orient="records")
        ],
    }
