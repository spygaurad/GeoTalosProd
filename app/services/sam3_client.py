"""HTTP client for SAM3 inference endpoints.

Reads configuration from the AIModel record:
    - endpoint_url: base URL of SAM3 server
    - auth_config: {"type": "bearer" | "api_key" | "none", ...}
    - request_config: {"timeout_s": float, ...}

Expected SAM3 response shape:
    {
      "instances": [
        {"label": "<str>", "confidence": <float>, "geometry": <GeoJSON>}
      ],
      "mask_png_b64": "<base64 uint16 PNG — pixel values = instance_id>",
      "mask_width": <int>,
      "mask_height": <int>
    }
"""

from __future__ import annotations

import logging

import httpx

from app.models.ai_model import AIModel
from app.schemas.inference import SAM3PromptPCS, SAM3PromptPVS

logger = logging.getLogger(__name__)


class SAM3Client:
    def __init__(self, model: AIModel, timeout_s: float | None = None) -> None:
        if not model.endpoint_url:
            raise ValueError(f"AIModel {model.id} has no endpoint_url")
        self.base_url = model.endpoint_url.rstrip("/")
        request_config = model.request_config or {}
        self.timeout = float(timeout_s or request_config.get("timeout_s", 300.0))
        self.headers = self._build_auth_headers(model.auth_config or {})

    @staticmethod
    def _build_auth_headers(auth: dict) -> dict:
        auth_type = auth.get("type", "none")
        if auth_type == "bearer":
            token = auth.get("token", "")
            return {"Authorization": f"Bearer {token}"} if token else {}
        if auth_type == "api_key":
            header = auth.get("header", "X-API-Key")
            key = auth.get("key", "")
            return {header: key} if key else {}
        return {}

    def run_pcs(
        self,
        image_b64_tiff: str,
        aoi_bbox_4326: list[float],
        prompt: SAM3PromptPCS,
        confidence_threshold: float,
        return_format: str,
    ) -> dict:
        """Call /pcs for Concept Segmentation. return_format: 'geojson' | 'mask_png_b64' | 'both'."""
        payload = {
            "image_b64_tiff": image_b64_tiff,
            "bbox_4326": aoi_bbox_4326,
            "text_phrases": prompt.text_phrases,
            "exemplar_s3_uris": prompt.exemplar_s3_uris,
            "confidence_threshold": confidence_threshold,
            "return_format": return_format,
        }
        return self._post("/pcs", payload)

    def run_pvs(
        self,
        image_b64_tiff: str,
        aoi_bbox_4326: list[float],
        pixel_points: list[list[float]] | None,
        point_labels: list[int] | None,
        pixel_boxes: list[list[float]] | None,
        confidence_threshold: float,
        return_format: str,
    ) -> dict:
        """Call /pvs for Visual Segmentation. Points/boxes are in pixel coordinates."""
        payload = {
            "image_b64_tiff": image_b64_tiff,
            "bbox_4326": aoi_bbox_4326,
            "points": pixel_points,
            "point_labels": point_labels,
            "boxes": pixel_boxes,
            "confidence_threshold": confidence_threshold,
            "return_format": return_format,
        }
        return self._post("/pvs", payload)

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=self.timeout, headers=self.headers) as client:
            response = client.post(url, json=payload)
            if response.status_code >= 400:
                logger.error(
                    "sam3_request_failed path=%s status=%s body=%s",
                    path, response.status_code, response.text[:500],
                )
            response.raise_for_status()
            return response.json()
