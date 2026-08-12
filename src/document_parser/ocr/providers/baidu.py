from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests


def load_env_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Environment file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value for {name}: {value}")


@dataclass(frozen=True)
class BaiduPaddleOcrConfig:
    api_key: str
    secret_key: str
    base_url: str = "https://aip.baidubce.com"
    token_path: str = "/oauth/2.0/token"
    submit_path: str = "/rest/2.0/brain/online/v2/paddle-vl-parser/task"
    query_path: str = "/rest/2.0/brain/online/v2/paddle-vl-parser/task/query"
    request_timeout_seconds: float = 300
    poll_interval_seconds: float = 6
    poll_timeout_seconds: float = 600
    merge_tables: bool = True
    relevel_titles: bool = True
    return_span_boxes: bool = True
    analysis_chart: bool = False
    recognize_seal: bool = False

    @classmethod
    def from_environment(cls) -> "BaiduPaddleOcrConfig":
        return cls(
            api_key=_required_env("BAIDU_OCR_API_KEY"),
            secret_key=_required_env("BAIDU_OCR_SECRET_KEY"),
            base_url=os.getenv("BAIDU_OCR_BASE_URL", "https://aip.baidubce.com").rstrip("/"),
            token_path=os.getenv("BAIDU_OCR_TOKEN_PATH", "/oauth/2.0/token"),
            submit_path=os.getenv(
                "BAIDU_PADDLEOCR_SUBMIT_PATH",
                "/rest/2.0/brain/online/v2/paddle-vl-parser/task",
            ),
            query_path=os.getenv(
                "BAIDU_PADDLEOCR_QUERY_PATH",
                "/rest/2.0/brain/online/v2/paddle-vl-parser/task/query",
            ),
            request_timeout_seconds=float(os.getenv("BAIDU_OCR_REQUEST_TIMEOUT_SECONDS", "300")),
            poll_interval_seconds=float(os.getenv("BAIDU_OCR_POLL_INTERVAL_SECONDS", "6")),
            poll_timeout_seconds=float(os.getenv("BAIDU_OCR_POLL_TIMEOUT_SECONDS", "600")),
            merge_tables=_env_bool("BAIDU_OCR_MERGE_TABLES", True),
            relevel_titles=_env_bool("BAIDU_OCR_RELEVEL_TITLES", True),
            return_span_boxes=_env_bool("BAIDU_OCR_RETURN_SPAN_BOXES", True),
            analysis_chart=_env_bool("BAIDU_OCR_ANALYSIS_CHART", False),
            recognize_seal=_env_bool("BAIDU_OCR_RECOGNIZE_SEAL", False),
        )

    def safe_options(self) -> dict[str, Any]:
        values = asdict(self)
        values.pop("api_key")
        values.pop("secret_key")
        return values


class BaiduPaddleOcrProvider:
    provider_name = "baidu_paddleocr_vl"

    def __init__(
        self,
        config: BaiduPaddleOcrConfig,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()

    @classmethod
    def from_env_file(cls, env_path: Path) -> "BaiduPaddleOcrProvider":
        load_env_file(env_path)
        return cls(BaiduPaddleOcrConfig.from_environment())

    def parse_image(self, image_path: Path) -> dict[str, Any]:
        access_token = self._access_token()
        submit = self._submit(access_token, image_path)
        task_id = str((submit.get("result") or {}).get("task_id", ""))
        if not task_id:
            raise RuntimeError("PaddleOCR-VL submit response did not contain task_id")
        final, history = self._poll(access_token, task_id)
        result = final.get("result") or {}
        parse_url = str(result.get("parse_result_url", ""))
        if not parse_url:
            raise RuntimeError("PaddleOCR-VL result did not contain parse_result_url")
        response = self._request(
            "PaddleOCR-VL result download",
            lambda: self.session.get(parse_url, timeout=self.config.request_timeout_seconds),
        )
        try:
            parse_result = response.json()
        except ValueError as error:
            raise RuntimeError("PaddleOCR-VL parse result was not valid JSON") from error
        return {
            "provider": self.provider_name,
            "task_id": task_id,
            "poll_history": history,
            "options": self.config.safe_options(),
            "parse_result": redact_signed_urls(parse_result),
        }

    def _access_token(self) -> str:
        response = self._request(
            "Baidu OAuth",
            lambda: self.session.post(
                f"{self.config.base_url}{self.config.token_path}",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.config.api_key,
                    "client_secret": self.config.secret_key,
                },
                timeout=self.config.request_timeout_seconds,
            ),
        )
        payload = self._json(response, "Baidu OAuth")
        token = str(payload.get("access_token", ""))
        if not token:
            raise RuntimeError("Baidu OAuth response did not contain access_token")
        return token

    def _submit(self, access_token: str, image_path: Path) -> dict[str, Any]:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        response = self._request(
            "PaddleOCR-VL submit",
            lambda: self.session.post(
                f"{self.config.base_url}{self.config.submit_path}",
                params={"access_token": access_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "file_data": encoded,
                    "file_name": image_path.name,
                    "analysis_chart": str(self.config.analysis_chart).lower(),
                    "merge_tables": str(self.config.merge_tables).lower(),
                    "relevel_titles": str(self.config.relevel_titles).lower(),
                    "recognize_seal": str(self.config.recognize_seal).lower(),
                    "return_span_boxes": str(self.config.return_span_boxes).lower(),
                },
                timeout=self.config.request_timeout_seconds,
            ),
        )
        return self._json(response, "PaddleOCR-VL submit")

    def _poll(self, access_token: str, task_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        started = time.monotonic()
        history = []
        while True:
            response = self._request(
                "PaddleOCR-VL query",
                lambda: self.session.post(
                    f"{self.config.base_url}{self.config.query_path}",
                    params={"access_token": access_token},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={"task_id": task_id},
                    timeout=self.config.request_timeout_seconds,
                ),
            )
            payload = self._json(response, "PaddleOCR-VL query")
            result = payload.get("result") or {}
            status = str(result.get("status", "unknown"))
            elapsed = round(time.monotonic() - started, 2)
            history.append({"elapsed_seconds": elapsed, "status": status})
            if status == "success":
                return payload, history
            if status == "failed":
                raise RuntimeError(f"PaddleOCR-VL task failed: {result.get('task_error', '')}")
            if elapsed >= self.config.poll_timeout_seconds:
                raise TimeoutError("PaddleOCR-VL task exceeded poll timeout")
            time.sleep(self.config.poll_interval_seconds)

    def _request(self, operation: str, callback: Any) -> requests.Response:
        try:
            response = callback()
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            raise RuntimeError(f"{operation} failed due to a network or HTTP client error") from error

    def _json(self, response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError(f"{operation} returned a non-JSON response") from error
        error_code = payload.get("error_code")
        if error_code not in {None, 0, "0"}:
            raise RuntimeError(
                f"{operation} failed: error_code={error_code}, "
                f"error_msg={payload.get('error_msg', '')}"
            )
        return payload


def redact_signed_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): redact_signed_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_signed_urls(item) for item in value]
    if isinstance(value, str):
        def replace_url(match: re.Match[str]) -> str:
            url = match.group(0)
            parts = urlsplit(url)
            if not parts.query:
                return url
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

        return re.sub(r"https?://[^\s\"'<>]+", replace_url, value)
    return value
