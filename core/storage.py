
import os
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY
import logging

# With the library defaults (one request, 120 s retry deadline) a large upload can time out
# on a slow link. Resumable upload in 8 MB pieces: each piece is its own short request, a
# failed piece is retried, and the whole upload may take up to UPLOAD_DEADLINE_S.
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_REQUEST_TIMEOUT_S = 300
UPLOAD_DEADLINE_S = 3600


def upload_to_gcs(file_path: str, destination_blob_name: str, bucket_name: str) -> str:
    """
    Uploads a file to the bucket and returns the gs:// URI.
    """
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name, chunk_size=UPLOAD_CHUNK_BYTES)

    print(f"   ☁️  Uploading {file_path} to gs://{bucket_name}/{destination_blob_name}...")
    blob.upload_from_filename(file_path, timeout=UPLOAD_REQUEST_TIMEOUT_S,
                              retry=DEFAULT_RETRY.with_deadline(UPLOAD_DEADLINE_S))

    return f"gs://{bucket_name}/{destination_blob_name}"

def download_gcs_prefix_to_file(gcs_prefix: str, destination_file_path: str, bucket_name: str):
    """
    Downloads all blobs matching a prefix and merges them into a single local file.
    Useful for Vertex AI Batch output which is split into multiple JSONL files.
    """
    # Clean prefix
    if gcs_prefix.startswith("gs://"):
        parts = gcs_prefix.split("/", 3)
        if len(parts) > 3:
            gcs_prefix = parts[3]
            
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    print(f"   📥 Downloading merged output from gs://{bucket_name}/{gcs_prefix}...")
    
    # List all blobs
    blobs = list(bucket.list_blobs(prefix=gcs_prefix))
    
    # Filter for valid output files (usually .jsonl or .json) 
    # and exclude potential directory markers or empty files
    data_blobs = [b for b in blobs if not b.name.endswith("/") and b.size > 0]
    
    if not data_blobs:
        print("      ⚠️ No data files found in GCS output directory.")
        # Create empty file to avoid crashes
        with open(destination_file_path, 'w') as f: pass
        return

    # Sort to ensure deterministic order if relevant (files usually numbered)
    data_blobs.sort(key=lambda x: x.name)
    
    with open(destination_file_path, "wb") as outfile:
        for blob in data_blobs:
            print(f"      + Merging {blob.name}")
            blob.download_to_file(outfile)
