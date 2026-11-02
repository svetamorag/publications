# %% [markdown]
# # DICOM De-ID Verification & PHI Audit — Colab Enterprise Batch Pipeline
#
# This **Google Cloud Colab Enterprise** notebook runs the dual-layer **DICOM De-Identification (HIPAA Safe Harbor & DICOM PS3.15 Annex E) Verification Pipeline** across all DICOM (`.dcm` / `.dicom`) images in a specified **Google Cloud Storage (GCS) bucket or folder**.
#
# ### What This Pipeline Does
# 1. **Discovers DICOM Objects**: Connects to the target GCS bucket (or subfolder prefix) and locates all `.dcm` / `.dicom` files.
# 2. **Layer 1 — Metadata & Private Tag Pre-Audit**: Extracts all DICOM header tags and flags known HIPAA Safe Harbor / DICOM PS3.15 Annex E identifiers and unverified private vendor tags.
# 3. **Layer 2 — Multimodal Pixel Burned-In PHI Verification**: Renders the DICOM pixel array (applying `RescaleSlope`/`RescaleIntercept` and `WindowCenter`/`WindowWidth`) and inspects both header metadata and image pixels using **Vertex AI Gemini (`gemini-3.8-flash`)**.
# 4. **Per-DICOM Cost Tracking**: Captures exact prompt (text + image) and candidate output token usage from Vertex AI `usage_metadata` along with GCS I/O operations to compute the exact cost per DICOM image and total batch cost.
# 5. **Single Consolidated JSON Report in GCS**: Saves one comprehensive JSON report for the entire batch directly back into the **same GCS bucket**, containing:
#    - Processing status (`PROCESSED` / `ERROR`)
#    - Verification status (`has_phi_pii`: `true`/`false`, `is_deidentified`: `true`/`false`, risk level, confidence score, burned-in PHI status)
#    - Detailed **PHI & Sensitivity Audit Findings** (`source`, `identifier_type`, `tag_or_location`, `detected_value`, `masked_value`, `severity`, `explanation`, `remediation`)
#    - **Costs per DICOM** (token breakdown, model used, latency, and USD cost)

# %%
# @title 1. Install Dependencies (Uncomment in Colab Enterprise if not pre-installed)
# %pip install --quiet google-genai>=2.24.0 google-cloud-storage>=3.13.1 pydicom>=3.0.2 Pillow>=12.3.0 numpy>=2.5.3 pydantic>=2.0.0

# %%
# @title 2. Configure GCP Project, Target GCS Bucket/Folder & Pricing Parameters
from collections import defaultdict
import datetime
import io
import json
import logging
import os
import re
import time
from typing import Any
import uuid

from google import genai
from google.cloud import storage
from google.genai import types
import numpy as np
from PIL import Image
from pydantic import BaseModel, Field
import pydicom
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.valuerep import PersonName

# --- Colab Enterprise Interactive Parameters ---
PROJECT_ID = "your-project-id"  # @param {type:"string"}
VERTEX_LOCATION = "global"  # @param {type:"string"}

# Enter a bucket name or gs:// URI pointing to a bucket or folder inside a bucket
# Examples: "gs://ccai_bucket_52788/dicom-samples/", "gs://ccai_bucket_52788", "ccai_bucket_52788/dicom-samples"
GCS_TARGET_URI = "gs://your_gcp_bucket/folder_name/"  # @param {type:"string"}

# Optional custom blob path for the output JSON inside the same bucket (leave empty to auto-generate with timestamp)
CUSTOM_OUTPUT_JSON_BLOB = ""  # @param {type:"string"}

# Whether to redact raw detected_value strings in the saved JSON report (masked_value is always included)
REDACT_RAW_PHI_IN_JSON = False  # @param {type:"boolean"}

PRIMARY_MODEL = "gemini-3.8-flash"  # @param {type:"string"}
FALLBACK_MODEL = "gemini-3.7-flash"  # @param {type:"string"}
MAX_DICOM_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB safety limit per DICOM

# Vertex AI Gemini Flash & GCS Pricing Rates (USD) for Per-DICOM Cost Calculation
INPUT_COST_PER_1M_TOKENS_USD = 0.15  # @param {type:"number"}
OUTPUT_COST_PER_1M_TOKENS_USD = 0.60  # @param {type:"number"}
THINKING_COST_PER_1M_TOKENS_USD = 0.60  # @param {type:"number"}
GCS_CLASS_B_READ_PER_10K_USD = 0.004  # Standard GCS Class B (GET object) per 10,000 ops

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dicom_batch_verifier")


def parse_gcs_address(gcs_address: str) -> tuple[str, str]:
    """Parse a GCS bucket or folder address into (bucket_name, folder_prefix).

    Accepts:
      - 'gs://my-bucket/path/to/folder/' -> ('my-bucket', 'path/to/folder/')
      - 'gs://my-bucket'                 -> ('my-bucket', '')
      - 'my-bucket/path/to/folder'       -> ('my-bucket', 'path/to/folder/')
      - 'my-bucket'                      -> ('my-bucket', '')
    """
    cleaned = gcs_address.strip()
    if cleaned.startswith("gs://"):
        cleaned = cleaned[len("gs://") :]
    cleaned = cleaned.lstrip("/")

    if "/" in cleaned:
        bucket_name, prefix = cleaned.split("/", 1)
        prefix = prefix.strip("/")
        if prefix:
            prefix = f"{prefix}/"
    else:
        bucket_name = cleaned
        prefix = ""

    if not re.match(r"^[a-z0-9][a-z0-9._\-]{1,100}[a-z0-9]$", bucket_name):
        raise ValueError(f"Invalid GCS bucket name: '{bucket_name}'")
    if ".." in prefix or prefix.startswith("/"):
        raise ValueError(f"Invalid GCS folder prefix: '{prefix}'")

    return bucket_name, prefix


BUCKET_NAME, FOLDER_PREFIX = parse_gcs_address(GCS_TARGET_URI)
print(f"Configured Target Bucket : gs://{BUCKET_NAME}")
print(f"Configured Folder Prefix : '{FOLDER_PREFIX or '(entire bucket)'}'")
print(f"Configured Vertex Model  : {PRIMARY_MODEL} (fallback: {FALLBACK_MODEL}) in project '{PROJECT_ID}'")


# %%
# @title 3. Define Dual-Layer DICOM Verification Pipeline & Cost Calculator

# Known HIPAA Safe Harbor / DICOM PS3.15 Annex E sensitive tags
PHI_TAG_RULES: dict[str, dict[str, str]] = {
    "PatientName": {"category": "Direct Patient Identifier", "severity": "CRITICAL"},
    "PatientID": {"category": "Medical Record Identifier", "severity": "HIGH"},
    "PatientBirthDate": {"category": "Date / Age Identifier", "severity": "CRITICAL"},
    "PatientBirthTime": {"category": "Date / Age Identifier", "severity": "HIGH"},
    "OtherPatientIDs": {"category": "Medical Record Identifier", "severity": "CRITICAL"},
    "OtherPatientNames": {"category": "Direct Patient Identifier", "severity": "CRITICAL"},
    "PatientAddress": {"category": "Geographic / Contact PHI", "severity": "CRITICAL"},
    "PatientTelephoneNumbers": {"category": "Geographic / Contact PHI", "severity": "CRITICAL"},
    "PatientMotherBirthName": {"category": "Direct Patient Identifier", "severity": "CRITICAL"},
    "MilitaryRank": {"category": "Demographic Identifier", "severity": "HIGH"},
    "BranchOfService": {"category": "Demographic Identifier", "severity": "HIGH"},
    "PatientInsurancePlanCodeSequence": {"category": "Health Plan Beneficiary", "severity": "CRITICAL"},
    "PatientComments": {"category": "Free-Text Clinical Note", "severity": "HIGH"},
    "AdditionalPatientHistory": {"category": "Free-Text Clinical Note", "severity": "HIGH"},
    "AccessionNumber": {"category": "Study / Visit Identifier", "severity": "HIGH"},
    "InstitutionName": {"category": "Facility / Provider Identifier", "severity": "HIGH"},
    "InstitutionAddress": {"category": "Facility / Provider Identifier", "severity": "HIGH"},
    "ReferringPhysicianName": {"category": "Physician / Staff Identifier", "severity": "HIGH"},
    "ReferringPhysicianAddress": {"category": "Physician / Staff Identifier", "severity": "HIGH"},
    "ReferringPhysicianTelephoneNumbers": {"category": "Physician / Staff Identifier", "severity": "HIGH"},
    "PerformingPhysicianName": {"category": "Physician / Staff Identifier", "severity": "HIGH"},
    "NameOfPhysiciansReadingStudy": {"category": "Physician / Staff Identifier", "severity": "HIGH"},
    "OperatorsName": {"category": "Physician / Staff Identifier", "severity": "MEDIUM"},
    "RequestingPhysician": {"category": "Physician / Staff Identifier", "severity": "HIGH"},
    "DeviceSerialNumber": {"category": "Device / Asset Identifier", "severity": "MEDIUM"},
    "StationName": {"category": "Device / Asset Identifier", "severity": "MEDIUM"},
    "StudyID": {"category": "Study / Visit Identifier", "severity": "MEDIUM"},
    "StudyDate": {"category": "Study / Acquisition Date", "severity": "MEDIUM"},
    "SeriesDate": {"category": "Study / Acquisition Date", "severity": "MEDIUM"},
    "AcquisitionDate": {"category": "Study / Acquisition Date", "severity": "MEDIUM"},
    "ContentDate": {"category": "Study / Acquisition Date", "severity": "MEDIUM"},
}

SAFE_PLACEHOLDER_PATTERNS = re.compile(
    r"^(anon|anonymous|redacted|deid|de-id|removed|none|unknown|dummy|scrubbed|test|00000000|19000101|19700101|20250101)([_\-\^a-z0-9\s\(\)\.]*)$",
    re.IGNORECASE,
)


class FindingItem(BaseModel):
    source: str = Field(description="Either 'METADATA_TAG', 'PRIVATE_TAG', or 'BURNED_IN_PIXEL'")
    identifier_type: str = Field(description="Category of PHI, e.g. Patient Name, DOB, MRN, Facility, Burned-in Text")
    tag_or_location: str = Field(description="DICOM tag keyword/ID or visual region in image (e.g. 'Top-left banner')")
    detected_value: str = Field(description="The sensitive text or value detected")
    severity: str = Field(description="One of 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'")
    explanation: str = Field(description="Why this violates HIPAA Safe Harbor / DICOM PS3.15 De-ID standards")
    remediation: str = Field(description="Specific action to remediate this finding")


class GeminiDicomVerificationSchema(BaseModel):
    is_deidentified: bool = Field(
        description="True ONLY if both metadata tags and pixel image are completely free of PHI/PII"
    )
    deid_confidence_score: int = Field(
        description="Compliance score from 0 (severe PHI exposure) to 100 (fully de-identified)"
    )
    overall_risk_level: str = Field(
        description="One of 'COMPLIANT', 'LOW_RISK', 'HIGH_RISK', 'CRITICAL_PHI_EXPOSURE'"
    )
    deid_executive_summary: str = Field(
        description="Concise summary of the De-ID audit across DICOM header tags and pixel imagery"
    )
    burned_in_phi_detected: bool = Field(
        description="True if any visible text/annotations containing patient, date, facility, or ID appear in pixel image"
    )
    burned_in_text_transcriptions: list[str] = Field(
        description="Exact strings read visually from the pixel image, if any"
    )
    phi_findings: list[FindingItem] = Field(
        description="Comprehensive list of all PHI/PII violations in metadata tags or burned-in pixels"
    )
    clinical_modality_summary: str = Field(
        description="Modality, anatomical region, view/plane, and imaging sequence description"
    )
    visual_observations: list[str] = Field(
        description="Key radiological/anatomical structures and technical characteristics visible in the image"
    )
    acquisition_insights: list[str] = Field(
        description="Insights derived from acquisition parameters (kVp, slice thickness, field strength, matrix, pixel spacing, etc.)"
    )
    recommended_deid_actions: list[str] = Field(
        description="Ordered actionable steps to achieve full DICOM PS3.15 Annex E & pixel De-ID compliance"
    )


def _mask_sensitive_string(val: str) -> str:
    """Create a masked preview of a potentially sensitive value for safe audit reporting."""
    cleaned = val.strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 4:
        return "***"
    return f"***REDACTED*** ({cleaned[:2]}•••{cleaned[-2:]})"


def _classify_tag_group(group_num: int, is_private: bool) -> str:
    if is_private:
        return "Private / Vendor Tags"
    mapping = {
        0x0002: "File Meta Information",
        0x0008: "Study & General Information",
        0x0010: "Patient Demographics",
        0x0018: "Acquisition & Scanner Parameters",
        0x0020: "Spatial & Series Coordinates",
        0x0028: "Image & Pixel Characteristics",
        0x0032: "Study Scheduling & Visit",
        0x0038: "Admission & Visit",
        0x0040: "Performed Procedure Step",
    }
    return mapping.get(group_num, "Additional Clinical Metadata")


def _format_elem_value(elem: pydicom.DataElement) -> str:
    if elem.tag == (0x7FE0, 0x0010):
        return f"<Binary Pixel Data: {len(elem.value)} bytes>"
    if elem.VR == "SQ":
        return f"<Sequence with {len(elem.value)} item(s)>"
    val = elem.value
    if val is None:
        return ""
    if isinstance(val, bytes):
        try:
            decoded = val.decode("utf-8", errors="ignore").strip()
            return decoded[:200] if decoded else f"<{len(val)} bytes>"
        except Exception:
            return f"<{len(val)} bytes>"
    if isinstance(val, MultiValue):
        return ", ".join(str(x) for x in val)
    if isinstance(val, PersonName):
        return str(val)
    text = str(val).strip()
    return text[:300]


def _extract_dicom_metadata_and_preaudit(ds: Dataset) -> dict[str, Any]:
    """Extract all DICOM tags, group them logically, and perform rule-based pre-audit."""
    tags_list: list[dict[str, Any]] = []
    grouped_tags: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rule_findings: list[dict[str, Any]] = []

    def inspect_dataset(dataset: Dataset, prefix: str = "") -> None:
        for elem in dataset:
            if elem.tag == (0x7FE0, 0x0010):
                continue
            tag_hex = f"({elem.tag.group:04X},{elem.tag.element:04X})"
            keyword = elem.keyword or elem.name or "PrivateTag"
            display_name = f"{prefix}{elem.name}" if prefix else elem.name
            is_private = elem.tag.is_private
            value_str = _format_elem_value(elem)
            group_label = _classify_tag_group(elem.tag.group, is_private)

            is_flagged = False
            flag_severity = "NONE"
            flag_reason = ""

            if is_private and value_str and not value_str.startswith("<"):
                if not SAFE_PLACEHOLDER_PATTERNS.match(value_str) and len(value_str) > 3:
                    is_flagged = True
                    flag_severity = "HIGH"
                    flag_reason = "Private vendor tag present; DICOM PS3.15 requires removing unverified private tags."
                    rule_findings.append(
                        {
                            "source": "PRIVATE_TAG",
                            "identifier_type": "Private Vendor Tag",
                            "tag_or_location": f"{tag_hex} {display_name}",
                            "detected_value": _mask_sensitive_string(value_str) if REDACT_RAW_PHI_IN_JSON else value_str,
                            "masked_value": _mask_sensitive_string(value_str),
                            "severity": "HIGH",
                            "explanation": flag_reason,
                            "remediation": "Strip private tags using pydicom remove_private_tags() or allow-list known safe vendor tags.",
                        }
                    )

            elif keyword in PHI_TAG_RULES and value_str:
                rule = PHI_TAG_RULES[keyword]
                if not SAFE_PLACEHOLDER_PATTERNS.match(value_str):
                    is_flagged = True
                    flag_severity = rule["severity"]
                    flag_reason = f"Contains non-scrubbed {rule['category']} ({keyword})."
                    rule_findings.append(
                        {
                            "source": "METADATA_TAG",
                            "identifier_type": rule["category"],
                            "tag_or_location": f"{tag_hex} {keyword}",
                            "detected_value": _mask_sensitive_string(value_str) if REDACT_RAW_PHI_IN_JSON else value_str,
                            "masked_value": _mask_sensitive_string(value_str),
                            "severity": flag_severity,
                            "explanation": f"Tag {tag_hex} ({keyword}) contains a value that is not a recognized anonymized placeholder.",
                            "remediation": f"Replace {keyword} with a dummy/anonymized value or clear it per DICOM PS3.15 Annex E.",
                        }
                    )

            elif keyword == "BurnedInAnnotation" and value_str.upper() == "YES":
                is_flagged = True
                flag_severity = "CRITICAL"
                flag_reason = "BurnedInAnnotation (0028,0301) is set to YES, indicating pixel PHI risk."

            elif keyword == "PatientIdentityRemoved" and value_str.upper() == "NO":
                is_flagged = True
                flag_severity = "HIGH"
                flag_reason = "PatientIdentityRemoved (0012,0062) explicitly states NO."

            entry = {
                "tag": tag_hex,
                "keyword": keyword,
                "name": display_name,
                "vr": elem.VR or "UN",
                "value": value_str,
                "masked_value": _mask_sensitive_string(value_str) if is_flagged else value_str,
                "group": group_label,
                "is_private": is_private,
                "is_flagged_phi": is_flagged,
                "severity": flag_severity,
                "flag_reason": flag_reason,
            }
            tags_list.append(entry)
            grouped_tags[group_label].append(entry)

            if elem.VR == "SQ":
                for idx, seq_item in enumerate(elem.value):
                    inspect_dataset(seq_item, prefix=f"{keyword}[{idx}].")

    if hasattr(ds, "file_meta") and ds.file_meta:
        inspect_dataset(ds.file_meta, prefix="Meta.")
    inspect_dataset(ds)

    summary_cards = {
        "modality": str(getattr(ds, "Modality", "OT")),
        "body_part": str(getattr(ds, "BodyPartExamined", "Unspecified")),
        "study_description": str(getattr(ds, "StudyDescription", "Unspecified")),
        "series_description": str(getattr(ds, "SeriesDescription", "Unspecified")),
        "manufacturer": str(getattr(ds, "Manufacturer", "Unknown")),
        "model_name": str(getattr(ds, "ManufacturerModelName", "Unknown")),
        "rows": int(getattr(ds, "Rows", 0) or 0),
        "columns": int(getattr(ds, "Columns", 0) or 0),
        "bits_allocated": int(getattr(ds, "BitsAllocated", 0) or 0),
        "bits_stored": int(getattr(ds, "BitsStored", 0) or 0),
        "photometric": str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")),
        "patient_identity_removed_tag": str(getattr(ds, "PatientIdentityRemoved", "MISSING")),
        "deid_method_tag": str(getattr(ds, "DeidentificationMethod", "Not Specified")),
        "burned_in_annotation_tag": str(getattr(ds, "BurnedInAnnotation", "MISSING")),
        "total_tags_extracted": len(tags_list),
        "private_tags_count": sum(1 for t in tags_list if t["is_private"]),
        "flagged_tags_count": sum(1 for t in tags_list if t["is_flagged_phi"]),
    }

    return {
        "summary": summary_cards,
        "tags": tags_list,
        "grouped_tags": dict(grouped_tags),
        "rule_findings": rule_findings,
    }


def _render_dicom_to_png_bytes(ds: Dataset) -> tuple[bytes, dict[str, Any]]:
    """Convert DICOM pixel_array into an 8-bit normalized PNG for Gemini multimodal vision."""
    try:
        arr = ds.pixel_array
    except Exception as exc:
        raise ValueError(f"Unable to decode DICOM pixel data: {exc}") from exc

    num_frames = 1
    if arr.ndim == 3:
        if arr.shape[-1] in (3, 4):
            pass
        else:
            num_frames = int(arr.shape[0])
            arr = arr[num_frames // 2]
    elif arr.ndim > 3:
        arr = arr[0, 0]

    arr_float = arr.astype(np.float32)

    slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
    intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
    arr_float = arr_float * slope + intercept

    min_hu = float(np.min(arr_float))
    max_hu = float(np.max(arr_float))
    mean_hu = float(np.mean(arr_float))
    std_hu = float(np.std(arr_float))

    wc = getattr(ds, "WindowCenter", None)
    ww = getattr(ds, "WindowWidth", None)
    if isinstance(wc, MultiValue):
        wc = float(wc[0])
    if isinstance(ww, MultiValue):
        ww = float(ww[0])

    if wc is not None and ww is not None and float(ww) > 1.0:
        center = float(wc)
        width = float(ww)
        low = center - width / 2.0
        high = center + width / 2.0
        clipped = np.clip(arr_float, low, high)
        norm = (clipped - low) / (high - low) * 255.0
    else:
        if max_hu > min_hu:
            norm = (arr_float - min_hu) / (max_hu - min_hu) * 255.0
        else:
            norm = np.zeros_like(arr_float)

    if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        norm = 255.0 - norm

    norm_uint8 = np.clip(norm, 0, 255).astype(np.uint8)
    pil_img = Image.fromarray(norm_uint8)
    if pil_img.mode not in ("L", "RGB"):
        pil_img = pil_img.convert("L")

    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    pixel_stats = {
        "width": int(pil_img.width),
        "height": int(pil_img.height),
        "num_frames": num_frames,
        "min_intensity": round(min_hu, 2),
        "max_intensity": round(max_hu, 2),
        "mean_intensity": round(mean_hu, 2),
        "std_intensity": round(std_hu, 2),
        "rescale_slope": slope,
        "rescale_intercept": intercept,
    }
    return png_bytes, pixel_stats


def _calculate_cost_metrics(usage_metadata: Any, model_name: str, latency_ms: int) -> dict[str, Any]:
    """Extract token usage from Gemini response and compute USD costs per DICOM."""
    prompt_tokens = int(getattr(usage_metadata, "prompt_token_count", 0) or 0) if usage_metadata else 0
    candidate_tokens = int(getattr(usage_metadata, "candidates_token_count", 0) or 0) if usage_metadata else 0
    thinking_tokens = int(getattr(usage_metadata, "thoughts_token_count", 0) or 0) if usage_metadata else 0
    total_tokens = (
        int(getattr(usage_metadata, "total_token_count", 0) or (prompt_tokens + candidate_tokens + thinking_tokens))
        if usage_metadata
        else 0
    )

    input_cost_usd = (prompt_tokens / 1_000_000.0) * INPUT_COST_PER_1M_TOKENS_USD
    output_cost_usd = (candidate_tokens / 1_000_000.0) * OUTPUT_COST_PER_1M_TOKENS_USD
    thinking_cost_usd = (thinking_tokens / 1_000_000.0) * THINKING_COST_PER_1M_TOKENS_USD
    gcs_io_cost_usd = GCS_CLASS_B_READ_PER_10K_USD / 10_000.0
    total_cost_usd = input_cost_usd + output_cost_usd + thinking_cost_usd + gcs_io_cost_usd

    return {
        "model_used": model_name,
        "input_tokens": prompt_tokens,
        "output_tokens": candidate_tokens,
        "thinking_tokens": thinking_tokens,
        "total_tokens": total_tokens,
        "input_cost_usd": round(input_cost_usd, 8),
        "output_cost_usd": round(output_cost_usd, 8),
        "thinking_cost_usd": round(thinking_cost_usd, 8),
        "gcs_io_cost_usd": round(gcs_io_cost_usd, 8),
        "total_cost_usd": round(total_cost_usd, 8),
        "latency_ms": latency_ms,
    }


def _run_gemini_verification_with_cost(
    genai_client: genai.Client,
    metadata_bundle: dict[str, Any],
    png_bytes: bytes,
    pixel_stats: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run Vertex AI Gemini multimodal verification and return (ai_report, cost_metrics)."""
    compact_tags = [
        {
            "tag": t["tag"],
            "keyword": t["keyword"],
            "vr": t["vr"],
            "value": t["value"],
            "is_private": t["is_private"],
            "preaudit_flagged": t["is_flagged_phi"],
        }
        for t in metadata_bundle["tags"]
        if t["value"]
    ]

    prompt = f"""You are a Senior Healthcare Privacy Officer (HIPAA Safe Harbor & DICOM PS3.15 Annex E expert) and Board-Certified Neuroradiologist / Body Radiologist.

Perform a comprehensive TWO-LAYER verification and clinical/technical analysis on the provided DICOM file:
1. LAYER 1 - METADATA DE-IDENTIFICATION VERIFICATION:
   Inspect all extracted DICOM header tags (including standard tags, free-text comments, UIDs, dates, institution/physician names, and private vendor tags) for any Protected Health Information (PHI) or Personally Identifiable Information (PII).
   Note: Values such as 'ANONYMOUS', 'REDACTED', 'ANON-...', or dummy dates '20250101' with PatientIdentityRemoved='YES' are considered properly de-identified placeholders. Real-looking names, dates of birth, street addresses, phone numbers, SSN/MRN strings, hospital names, doctor names, or private tags containing tracking names are violations.

2. LAYER 2 - VISUAL PIXEL BURNED-IN PHI VERIFICATION:
   Carefully inspect the attached DICOM image pixels (especially top and bottom banners/overlays) for any burned-in text annotations (e.g., patient names, DOB, MRN, hospital names, dates, or accession numbers).
   Even if the metadata header claims BurnedInAnnotation='NO' or PatientIdentityRemoved='YES', if there is visible burned-in PHI text on the image pixels, you MUST flag `is_deidentified = false` and `burned_in_phi_detected = true` and transcribe the burned-in strings!

3. LAYER 3 - CLINICAL & TECHNICAL AI INSIGHTS:
   Analyze the rendered DICOM image and acquisition metadata to provide:
   - Modality, anatomical region, imaging plane, and sequence characterization
   - Key visual/anatomical observations and image quality assessment
   - Technical acquisition parameter interpretation (resolution, bit depth, pixel statistics, contrast/windowing)
   - Actionable De-ID remediation recommendations

Extracted DICOM Summary:
{json.dumps(metadata_bundle["summary"], indent=2)}

Pixel Statistics:
{json.dumps(pixel_stats, indent=2)}

Extracted DICOM Header Tags:
{json.dumps(compact_tags, indent=2)}
"""

    image_part = types.Part.from_bytes(data=png_bytes, mime_type="image/png")
    models_to_try = [PRIMARY_MODEL, FALLBACK_MODEL]
    last_error = None

    for model_name in models_to_try:
        t0 = time.time()
        try:
            resp = genai_client.models.generate_content(
                model=model_name,
                contents=[image_part, prompt],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=GeminiDicomVerificationSchema,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            parsed = json.loads(resp.text)

            for finding in parsed.get("phi_findings", []):
                raw_val = finding.get("detected_value", "")
                finding["masked_value"] = _mask_sensitive_string(raw_val)
                if REDACT_RAW_PHI_IN_JSON:
                    finding["detected_value"] = finding["masked_value"]

            if REDACT_RAW_PHI_IN_JSON:
                parsed["burned_in_text_transcriptions"] = [
                    _mask_sensitive_string(line) for line in parsed.get("burned_in_text_transcriptions", [])
                ]

            cost_metrics = _calculate_cost_metrics(
                usage_metadata=getattr(resp, "usage_metadata", None),
                model_name=model_name,
                latency_ms=elapsed_ms,
            )
            return parsed, cost_metrics
        except Exception as exc:
            last_error = exc
            logger.warning("Model %s attempt failed (%s), checking fallback...", model_name, type(exc).__name__)

    raise RuntimeError(f"Gemini verification failed across models: {last_error}")


# %%
# @title 4. Run Batch Verification Across GCS Bucket/Folder & Save Consolidated JSON Report to Same Bucket


def _merge_audit_findings(
    ai_findings: list[dict[str, Any]],
    rule_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine Gemini multimodal PHI findings with deterministic rule-based pre-audit findings without duplicates."""
    merged = list(ai_findings)
    seen_keys = {
        (f.get("source", ""), f.get("tag_or_location", "").lower(), f.get("masked_value", "").lower())
        for f in merged
    }
    for rf in rule_findings:
        key = (rf.get("source", ""), rf.get("tag_or_location", "").lower(), rf.get("masked_value", "").lower())
        tag_code = rf.get("tag_or_location", "").split(" ")[0].lower()
        already_covered = any(tag_code and tag_code in existing.get("tag_or_location", "").lower() for existing in merged)
        if key not in seen_keys and not already_covered:
            merged.append(rf)
            seen_keys.add(key)
    return merged


def process_single_gcs_dicom_blob(
    blob: storage.Blob,
    bucket_name: str,
    genai_client: genai.Client,
) -> dict[str, Any]:
    """Download a single DICOM blob from GCS into memory, verify PHI/PII, and return structured audit + cost record."""
    gcs_uri = f"gs://{bucket_name}/{blob.name}"
    file_name = os.path.basename(blob.name)
    size_bytes = int(blob.size or 0)

    if size_bytes > MAX_DICOM_SIZE_BYTES:
        return {
            "blob_name": blob.name,
            "file_name": file_name,
            "gcs_uri": gcs_uri,
            "size_bytes": size_bytes,
            "status": "ERROR",
            "error_message": f"File size ({size_bytes} bytes) exceeds maximum limit of {MAX_DICOM_SIZE_BYTES} bytes.",
            "verification_status": None,
            "phi_and_sensitivity_audit_findings": None,
            "cost_per_dicom": {
                "model_used": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "total_tokens": 0,
                "input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "thinking_cost_usd": 0.0,
                "gcs_io_cost_usd": 0.0,
                "total_cost_usd": 0.0,
                "latency_ms": 0,
            },
        }

    try:
        raw_bytes = blob.download_as_bytes()
        ds = pydicom.dcmread(io.BytesIO(raw_bytes), force=False)
        metadata_bundle = _extract_dicom_metadata_and_preaudit(ds)
        png_bytes, pixel_stats = _render_dicom_to_png_bytes(ds)
        ai_report, cost_metrics = _run_gemini_verification_with_cost(
            genai_client=genai_client,
            metadata_bundle=metadata_bundle,
            png_bytes=png_bytes,
            pixel_stats=pixel_stats,
        )

        combined_findings = _merge_audit_findings(
            ai_findings=ai_report.get("phi_findings", []),
            rule_findings=metadata_bundle.get("rule_findings", []),
        )

        is_deidentified = bool(ai_report.get("is_deidentified", False)) and len(combined_findings) == 0
        has_phi_pii = not is_deidentified

        severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        source_counts = {"METADATA_TAG": 0, "PRIVATE_TAG": 0, "BURNED_IN_PIXEL": 0}
        for f in combined_findings:
            sev = str(f.get("severity", "HIGH")).upper()
            if sev in severity_counts:
                severity_counts[sev] += 1
            src = str(f.get("source", "METADATA_TAG")).upper()
            if src in source_counts:
                source_counts[src] += 1

        return {
            "blob_name": blob.name,
            "file_name": file_name,
            "gcs_uri": gcs_uri,
            "size_bytes": size_bytes,
            "status": "PROCESSED",
            "verification_status": {
                "has_phi_pii": has_phi_pii,
                "is_deidentified": is_deidentified,
                "verdict": "PHI_PII_DETECTED" if has_phi_pii else "CLEAN_DEIDENTIFIED",
                "overall_risk_level": ai_report.get("overall_risk_level", "UNKNOWN"),
                "deid_confidence_score": int(ai_report.get("deid_confidence_score", 0)),
                "burned_in_phi_detected": bool(ai_report.get("burned_in_phi_detected", False)),
                "burned_in_text_transcriptions": ai_report.get("burned_in_text_transcriptions", []),
                "executive_summary": ai_report.get("deid_executive_summary", ""),
            },
            "phi_and_sensitivity_audit_findings": {
                "findings_count": len(combined_findings),
                "severity_breakdown": severity_counts,
                "source_breakdown": source_counts,
                "findings": combined_findings,
                "rule_based_preaudit_findings": metadata_bundle.get("rule_findings", []),
                "recommended_deid_actions": ai_report.get("recommended_deid_actions", []),
            },
            "clinical_and_technical_metadata": {
                "dicom_header_summary": metadata_bundle.get("summary", {}),
                "pixel_statistics": pixel_stats,
                "clinical_modality_summary": ai_report.get("clinical_modality_summary", ""),
                "visual_observations": ai_report.get("visual_observations", []),
                "acquisition_insights": ai_report.get("acquisition_insights", []),
            },
            "cost_per_dicom": cost_metrics,
        }

    except Exception as exc:
        logger.error("Failed processing %s: %s", gcs_uri, type(exc).__name__)
        return {
            "blob_name": blob.name,
            "file_name": file_name,
            "gcs_uri": gcs_uri,
            "size_bytes": size_bytes,
            "status": "ERROR",
            "error_message": f"Processing error ({type(exc).__name__})",
            "verification_status": None,
            "phi_and_sensitivity_audit_findings": None,
            "cost_per_dicom": {
                "model_used": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "total_tokens": 0,
                "input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "thinking_cost_usd": 0.0,
                "gcs_io_cost_usd": round(GCS_CLASS_B_READ_PER_10K_USD / 10_000.0, 8),
                "total_cost_usd": round(GCS_CLASS_B_READ_PER_10K_USD / 10_000.0, 8),
                "latency_ms": 0,
            },
        }


def run_gcs_dicom_batch_verification(
    gcs_address: str,
    custom_output_blob: str = "",
) -> dict[str, Any]:
    """Discover all DICOM files in the GCS bucket/folder, run verification, and save one JSON report in the same bucket."""
    bucket_name, folder_prefix = parse_gcs_address(gcs_address)
    storage_client = storage.Client(project=PROJECT_ID)
    bucket = storage_client.bucket(bucket_name)

    genai_client = genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=VERTEX_LOCATION,
        http_options=types.HttpOptions(timeout=60000),
    )

    logger.info("Listing DICOM objects in gs://%s/%s ...", bucket_name, folder_prefix)
    all_blobs = list(storage_client.list_blobs(bucket, prefix=folder_prefix or None))
    dicom_blobs = [
        b
        for b in all_blobs
        if not b.name.endswith("/")
        and (b.name.lower().endswith(".dcm") or b.name.lower().endswith(".dicom"))
    ]

    logger.info("Found %d DICOM file(s) to process.", len(dicom_blobs))

    batch_id = f"batch-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    dicom_results: list[dict[str, Any]] = []

    for idx, blob in enumerate(dicom_blobs, start=1):
        logger.info("[%d/%d] Verifying gs://%s/%s ...", idx, len(dicom_blobs), bucket_name, blob.name)
        item_result = process_single_gcs_dicom_blob(blob, bucket_name, genai_client)
        dicom_results.append(item_result)

        if item_result["status"] == "PROCESSED":
            v_stat = item_result["verification_status"]
            f_cnt = item_result["phi_and_sensitivity_audit_findings"]["findings_count"]
            cost_val = item_result["cost_per_dicom"]["total_cost_usd"]
            logger.info(
                "   -> Status: %s | has_phi_pii=%s | Findings=%d | Cost=$%.6f",
                v_stat["verdict"],
                v_stat["has_phi_pii"],
                f_cnt,
                cost_val,
            )
        else:
            logger.warning("   -> Status: ERROR (%s)", item_result.get("error_message"))

    processed_items = [r for r in dicom_results if r["status"] == "PROCESSED"]
    error_items = [r for r in dicom_results if r["status"] != "PROCESSED"]
    phi_detected_items = [
        r for r in processed_items if r["verification_status"] and r["verification_status"]["has_phi_pii"]
    ]
    clean_items = [
        r for r in processed_items if r["verification_status"] and not r["verification_status"]["has_phi_pii"]
    ]

    total_input_tokens = sum(r["cost_per_dicom"]["input_tokens"] for r in dicom_results)
    total_output_tokens = sum(r["cost_per_dicom"]["output_tokens"] for r in dicom_results)
    total_thinking_tokens = sum(r["cost_per_dicom"]["thinking_tokens"] for r in dicom_results)
    total_tokens = sum(r["cost_per_dicom"]["total_tokens"] for r in dicom_results)
    total_cost_usd = round(sum(r["cost_per_dicom"]["total_cost_usd"] for r in dicom_results), 8)
    avg_cost_usd = round(total_cost_usd / len(dicom_results), 8) if dicom_results else 0.0

    # Determine output JSON blob path inside the SAME GCS bucket
    if custom_output_blob.strip():
        clean_custom = custom_output_blob.strip().lstrip("/")
        if ".." in clean_custom:
            raise ValueError("Invalid custom output JSON blob path.")
        output_blob_path = clean_custom
    else:
        timestamp_slug = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_blob_path = f"{folder_prefix}dicom_verification_batch_report_{timestamp_slug}.json"

    output_gcs_uri = f"gs://{bucket_name}/{output_blob_path}"

    batch_report = {
        "batch_id": batch_id,
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "gcp_project": PROJECT_ID,
        "target_gcs_address": gcs_address,
        "bucket_name": bucket_name,
        "folder_prefix": folder_prefix,
        "output_report_gcs_uri": output_gcs_uri,
        "batch_summary": {
            "total_dicom_files_discovered": len(dicom_blobs),
            "processed_successfully": len(processed_items),
            "failed_count": len(error_items),
            "verified_clean_count": len(clean_items),
            "phi_pii_detected_count": len(phi_detected_items),
        },
        "batch_cost_summary": {
            "primary_model": PRIMARY_MODEL,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_thinking_tokens": total_thinking_tokens,
            "total_tokens": total_tokens,
            "total_batch_cost_usd": total_cost_usd,
            "average_cost_per_dicom_usd": avg_cost_usd,
            "pricing_assumptions_usd": {
                "input_cost_per_1m_tokens_usd": INPUT_COST_PER_1M_TOKENS_USD,
                "output_cost_per_1m_tokens_usd": OUTPUT_COST_PER_1M_TOKENS_USD,
                "thinking_cost_per_1m_tokens_usd": THINKING_COST_PER_1M_TOKENS_USD,
                "gcs_class_b_read_per_10k_usd": GCS_CLASS_B_READ_PER_10K_USD,
            },
        },
        "dicom_results": dicom_results,
    }

    # Upload consolidated JSON report back to the same GCS bucket
    report_json_bytes = json.dumps(batch_report, indent=2).encode("utf-8")
    out_blob = bucket.blob(output_blob_path)
    out_blob.upload_from_string(report_json_bytes, content_type="application/json")
    logger.info("Batch verification complete! Consolidated JSON report saved to: %s", output_gcs_uri)

    return batch_report


# %%
# @title 5. Execute Batch Pipeline & Inspect Summary, PHI Audit Findings & Costs per DICOM
if __name__ == "__main__":
    batch_report = run_gcs_dicom_batch_verification(
        gcs_address=GCS_TARGET_URI,
        custom_output_blob=CUSTOM_OUTPUT_JSON_BLOB,
    )

    print("\n" + "=" * 95)
    print(f"BATCH ID              : {batch_report['batch_id']}")
    print(f"SOURCE GCS URI        : {batch_report['target_gcs_address']}")
    print(f"SAVED JSON REPORT URI : {batch_report['output_report_gcs_uri']}")
    print(f"TOTAL DICOMS FOUND    : {batch_report['batch_summary']['total_dicom_files_discovered']}")
    print(f"VERIFIED CLEAN        : {batch_report['batch_summary']['verified_clean_count']}")
    print(f"PHI / PII DETECTED    : {batch_report['batch_summary']['phi_pii_detected_count']}")
    print(f"TOTAL BATCH COST (USD): ${batch_report['batch_cost_summary']['total_batch_cost_usd']:.6f}")
    print("=" * 95)

    for item in batch_report.get("dicom_results", []):
        v = item.get("verification_status") or {}
        f_block = item.get("phi_and_sensitivity_audit_findings") or {}
        c = item.get("cost_per_dicom") or {}
        print(f"\nFile: {item['gcs_uri']}")
        print(
            f"  Status             : {item['status']}\n"
            f"  Has PHI/PII        : {v.get('has_phi_pii')} (Verdict: {v.get('verdict')})\n"
            f"  Risk Level & Score : {v.get('overall_risk_level')} ({v.get('deid_confidence_score')}/100)\n"
            f"  Burned-In Pixel PHI: {v.get('burned_in_phi_detected')}\n"
            f"  Cost per DICOM     : ${c.get('total_cost_usd', 0.0):.6f} "
            f"(In: {c.get('input_tokens', 0)} tok, Out: {c.get('output_tokens', 0)} tok, "
            f"Model: {c.get('model_used')}, Latency: {c.get('latency_ms', 0)} ms)\n"
            f"  Audit Findings ({f_block.get('findings_count', 0)}):"
        )
        findings = f_block.get("findings") or []
        if not findings:
            print("    ✓ No PHI/PII findings detected across metadata tags or image pixels.")
        else:
            for idx, finding in enumerate(findings, start=1):
                print(
                    f"    [{idx}] [{finding.get('severity')}] ({finding.get('source')}) "
                    f"{finding.get('identifier_type')} @ {finding.get('tag_or_location')}: "
                    f"{finding.get('masked_value')} -> {finding.get('explanation')}"
                )
