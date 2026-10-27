"""Fixed, bounded and side-effect-free demonstration job handlers."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
import struct
import time
import zlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

MAX_PAYLOAD_BYTES = 512 * 1024
MAX_RESULT_BYTES = 512 * 1024
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TEMPLATES = frozenset({"welcome", "receipt", "alert"})


class PermanentJobError(ValueError):
    """The payload or job type cannot succeed on a retry."""


class HandlerTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class ExecutionContext:
    deadline: float

    def checkpoint(self) -> None:
        if time.monotonic() >= self.deadline:
            raise HandlerTimeout("handler deadline exceeded")

    def pause(self, seconds: float) -> None:
        end = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            time.sleep(min(0.025, end - time.monotonic()))
            self.checkpoint()


def validate_payload(job_type: str, payload: Any) -> dict[str, Any]:
    if not isinstance(job_type, str) or job_type not in HANDLERS:
        raise PermanentJobError("job type is not allowlisted")
    if not isinstance(payload, dict):
        raise PermanentJobError("payload must be a JSON object")
    try:
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise PermanentJobError("payload must be JSON serializable") from None
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise PermanentJobError("payload exceeds the 512 KiB limit")
    handler = HANDLERS[job_type]
    handler.validate(payload)
    return payload


def _image_validate(payload: dict[str, Any]) -> None:
    if set(payload) != {"png_base64"}:
        raise PermanentJobError("image metadata accepts only png_base64")
    data = payload.get("png_base64")
    if not isinstance(data, str) or len(data) > MAX_PAYLOAD_BYTES:
        raise PermanentJobError("png_base64 must be a bounded base64 string")


def _image_metadata(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
    _image_validate(payload)
    try:
        image = base64.b64decode(payload["png_base64"], validate=True)
    except (binascii.Error, ValueError):
        raise PermanentJobError("invalid base64 image") from None
    context.checkpoint()
    # Read the bounded PNG header only; never invoke a decoder or touch the filesystem.
    if len(image) < 33 or image[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">I", image[8:12])[0] != 13 or image[12:16] != b"IHDR":
        raise PermanentJobError("only PNG images with an IHDR header are supported")
    ihdr = image[12:29]
    if zlib.crc32(ihdr) & 0xFFFFFFFF != struct.unpack(">I", image[29:33])[0]:
        raise PermanentJobError("PNG IHDR checksum is invalid")
    width, height, depth, color, compression, filter_method, interlace = struct.unpack(">IIBBBBB", image[16:29])
    if not width or not height or width > 100_000 or height > 100_000:
        raise PermanentJobError("PNG dimensions are invalid or exceed 100000 pixels")
    valid_depth = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
    if depth not in valid_depth.get(color, set()) or compression != 0 or filter_method != 0 or interlace not in (0, 1):
        raise PermanentJobError("PNG IHDR fields are invalid")
    return {"format": "png", "width": width, "height": height, "bit_depth": depth, "color_type": color, "encoded_bytes": len(image)}


def _csv_validate(payload: dict[str, Any]) -> None:
    if set(payload) != {"csv_text", "column"}:
        raise PermanentJobError("CSV transform accepts only csv_text and column")
    text, column = payload.get("csv_text"), payload.get("column")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 256 * 1024:
        raise PermanentJobError("csv_text must be a string no larger than 256 KiB")
    if not isinstance(column, str) or not _TOKEN.fullmatch(column):
        raise PermanentJobError("column must be a 1..64 character header token")


def _csv_transform(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
    _csv_validate(payload)
    reader = csv.DictReader(io.StringIO(payload["csv_text"], newline=""), strict=True)
    try:
        fieldnames = reader.fieldnames
    except csv.Error:
        raise PermanentJobError("CSV syntax is invalid") from None
    if not fieldnames or len(fieldnames) > 100 or len(set(fieldnames)) != len(fieldnames):
        raise PermanentJobError("CSV needs 1..100 unique headers")
    column = payload["column"]
    if column not in fieldnames:
        raise PermanentJobError("column is not present in CSV headers")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    count = 0
    try:
        for row in reader:
            count += 1
            if count > 10_000:
                raise PermanentJobError("CSV exceeds 10000 rows")
            context.checkpoint()
            if None in row:
                raise PermanentJobError("CSV row has more cells than its header")
            row[column] = (row[column] or "").strip().upper()
            writer.writerow(row)
            if output.tell() > MAX_RESULT_BYTES:
                raise PermanentJobError("transformed CSV exceeds 512 KiB")
    except csv.Error:
        raise PermanentJobError("CSV syntax is invalid") from None
    return {"rows": count, "column": column, "csv_text": output.getvalue()}


def _report_validate(payload: dict[str, Any]) -> None:
    if set(payload) - {"records", "work_ms"} or "records" not in payload:
        raise PermanentJobError("report accepts only records and optional work_ms")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) > 10_000:
        raise PermanentJobError("records must be a list of at most 10000 items")
    for row in records:
        if not isinstance(row, dict) or set(row) != {"category", "amount"} or not isinstance(row.get("category"), str) or not _TOKEN.fullmatch(row["category"]):
            raise PermanentJobError("each record requires a bounded category token")
        amount = row.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or abs(amount) > 1_000_000_000:
            raise PermanentJobError("each record amount must be a bounded integer")
    delay = payload.get("work_ms", 0)
    if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= 2_000:
        raise PermanentJobError("work_ms must be an integer from 0 to 2000")


def _synthetic_report(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
    _report_validate(payload)
    totals: dict[str, dict[str, int]] = {}
    for row in payload["records"]:
        context.checkpoint()
        entry = totals.setdefault(row["category"], {"count": 0, "total": 0})
        entry["count"] += 1
        entry["total"] += row["amount"]
    if payload.get("work_ms", 0):
        context.pause(payload["work_ms"] / 1000)
    return {"record_count": len(payload["records"]), "categories": dict(sorted(totals.items()))}


def _notification_validate(payload: dict[str, Any]) -> None:
    if set(payload) - {"template", "recipient_ref", "delay_ms"} or not {"template", "recipient_ref"}.issubset(payload):
        raise PermanentJobError("notification accepts only template, recipient_ref and optional delay_ms")
    template, recipient = payload.get("template"), payload.get("recipient_ref")
    if not isinstance(template, str) or template not in _TEMPLATES:
        raise PermanentJobError("template is not allowlisted")
    if not isinstance(recipient, str) or not _TOKEN.fullmatch(recipient):
        raise PermanentJobError("recipient_ref must be a bounded opaque token")
    delay = payload.get("delay_ms", 0)
    if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= 5_000:
        raise PermanentJobError("delay_ms must be an integer from 0 to 5000")


def _notification_simulation(payload: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
    _notification_validate(payload)
    if payload.get("delay_ms", 0):
        context.pause(payload["delay_ms"] / 1000)
    return {"delivery": "simulated", "template": payload["template"]}


@dataclass(frozen=True)
class Handler:
    validate: Callable[[dict[str, Any]], None]
    execute: Callable[[dict[str, Any], ExecutionContext], dict[str, Any]]


# Deliberately closed registry: payloads can select a name, never provide code.
HANDLERS: Mapping[str, Handler] = MappingProxyType({
    "image_metadata": Handler(_image_validate, _image_metadata),
    "csv_transform": Handler(_csv_validate, _csv_transform),
    "synthetic_report": Handler(_report_validate, _synthetic_report),
    "notification_simulation": Handler(_notification_validate, _notification_simulation),
})


def execute(job_type: str, payload: Any, timeout: float) -> dict[str, Any]:
    valid = validate_payload(job_type, payload)
    context = ExecutionContext(time.monotonic() + timeout)
    result = HANDLERS[job_type].execute(valid, context)
    context.checkpoint()
    if len(json.dumps(result, separators=(",", ":"), allow_nan=False).encode()) > MAX_RESULT_BYTES:
        raise PermanentJobError("handler result exceeds 512 KiB")
    return result
