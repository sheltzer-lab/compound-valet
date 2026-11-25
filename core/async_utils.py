import os
import time
from typing import Optional
from google.genai import types

from core.model_config import USE_VERTEX_AI, GCS_BUCKET_NAME
from core.client_factory import get_client
from core.storage import upload_to_gcs, download_gcs_prefix_to_file


def submit_batch_job(jsonl_path: str, model_name: str, display_name: str, api_key: str = None, dry_run: bool = False) -> str:
    """Uploads a JSONL file to Cloud Storage and submits a Vertex AI Gemini batch job."""
    print(f"   ☁️ Uploading {jsonl_path}...")
    if dry_run:
        print("   [DRY RUN] Skipping upload and submission.")
        return f"mock_job_{int(time.time())}"

    if not USE_VERTEX_AI:
        raise ValueError("Vertex AI (GOOGLE_CLOUD_PROJECT) is required for Gemini batch jobs.")
    if not GCS_BUCKET_NAME:
        raise ValueError("GCS_BUCKET_NAME must be set for Vertex AI batch jobs.")

    client = get_client(api_key=api_key)
    blob_name = f"batch_inputs/{display_name}/{os.path.basename(jsonl_path)}"
    gcs_uri = upload_to_gcs(jsonl_path, blob_name, GCS_BUCKET_NAME)

    print(f"   🚀 Submitting Batch (Vertex): {display_name}...")
    batch_job = client.batches.create(
        model=model_name,
        src=gcs_uri,
        config=types.CreateBatchJobConfig(display_name=display_name),
    )
    print(f"      Job ID: {batch_job.name}")
    return batch_job.name


def check_job_status(job_name: str, api_key: str = None, dry_run: bool = False) -> Optional[str]:
    """Checks the status of a batch job."""
    if not job_name:
        return None

    if dry_run and job_name.startswith("mock_job"):
        print(f"   [DRY RUN] Mock job {job_name} is COMPLETED.")
        return "COMPLETED"

    try:
        client = get_client(api_key=api_key)
        job = client.batches.get(name=job_name)
        print(f"   Job {job_name}: {job.state}")
        return str(job.state)
    except Exception as e:
        print(f"   ⚠️ Error checking job: {e}")
        return None


def _extract_vertex_output_uri(job) -> Optional[str]:
    """Locate the Cloud Storage output URI on a Vertex AI batch job object."""
    for attr in ("output_location", "output_uri", "destination"):
        val = getattr(job, attr, None)
        if val:
            return val
    dest = getattr(job, "dest", None)
    if dest:
        for sub_attr in ("gcs_uri", "uri", "file_name", "output_uri"):
            val = getattr(dest, sub_attr, None)
            if val:
                return val[0] if isinstance(val, list) else val
    return None


def download_batch_results(job_name: str, batch_dir: str, api_key: str = None, dry_run: bool = False) -> str:
    """Downloads the merged JSONL output of a completed Vertex AI batch job."""
    if dry_run and job_name.startswith("mock_job"):
        print("   [DRY RUN] Generating mock output file.")
        path = os.path.join(batch_dir, f"{job_name}_output.jsonl")
        with open(path, "w") as f:
            f.write("")
        return path

    client = get_client(api_key=api_key)
    job = client.batches.get(name=job_name)
    local_path = os.path.join(batch_dir, f"{job_name.split('/')[-1]}_output.jsonl")

    try:
        output_uri = _extract_vertex_output_uri(job)
        if not output_uri:
            print(f"   ⚠️ Could not determine Vertex output URI for {job_name}")
            return ""
        download_gcs_prefix_to_file(output_uri, local_path, GCS_BUCKET_NAME)
    except Exception as e:
        print(f"   ⚠️ Vertex Download Error: {e}")
        return ""

    return local_path
