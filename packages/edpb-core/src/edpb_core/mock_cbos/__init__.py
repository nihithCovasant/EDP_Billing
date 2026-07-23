"""THE mock CBOS server (v5 contract) — extracted from EDPBilling_FIle_Upload
(wayfinder tickets 12 + 06) so every repo tests against the SAME simulation.

Run:  uvicorn edpb_core.mock_cbos.app:app --port 8009
Needs the package's [mock] extra (fastapi/uvicorn/pydantic/python-multipart).
"""
