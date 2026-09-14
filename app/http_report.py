"""HTTP reporting layer."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .parser import ManifestError, parse_yaml
from .proof import analyze_graph
from .topology import TopologyBuildError, build_graph


logger = logging.getLogger("cable_splicing.http")


app = FastAPI(
    title="Cable Splicing Manifest Prover",
    version="1.0.0",
    description="End-to-end A/B polarity and pairing proof for paper-insulated telephone cable splice books.",
)


@app.get("/health", tags=["ops"])
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/", tags=["discovery"])
def index() -> dict[str, object]:
    return {
        "service": "cable-splicing-prover",
        "content_type": "application/yaml",
        "proof_endpoint": {"method": "POST", "path": "/prove"},
        "health_endpoint": {"method": "GET", "path": "/health"},
    }


@app.post("/prove", tags=["proof"])
async def prove(request: Request) -> Response:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/yaml", "application/x-yaml", "text/yaml"}:
        return JSONResponse(
            status_code=415,
            content={
                "status": "unsupported_media_type",
                "errors": [
                    {
                        "code": "unsupported_media_type",
                        "message": "request Content-Type must be application/yaml",
                    }
                ],
            },
        )

    raw = await request.body()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _structural_response([
            {"code": "invalid_yaml", "message": f"manifest must be UTF-8 text: {exc}", "location": ""}
        ])

    try:
        manifest = parse_yaml(text)
        graph = build_graph(manifest)
        report = analyze_graph(graph)
    except ManifestError as exc:
        return _structural_response(
            [
                {"code": issue.code, "message": issue.message, "location": issue.location}
                for issue in exc.issues
            ]
        )
    except TopologyBuildError as exc:
        # This normally represents a parser invariant failure; malformed input must still be 422.
        return _structural_response(
            [{"code": "topology_build_error", "message": str(exc), "location": ""}]
        )
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError) as exc:
        # Malformed manifest content must never escape as a bare 500 to the splicing engineer.
        logger.warning("rejected malformed manifest: %s", exc, exc_info=True)
        return _structural_response(
            [
                {
                    "code": "invalid_manifest",
                    "message": f"manifest could not be processed: {type(exc).__name__}: {exc}",
                    "location": "",
                }
            ]
        )

    try:
        return JSONResponse(status_code=200, content=report)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive serialization guard
        logger.error("report could not be serialized: %s", exc, exc_info=True)
        return _structural_response(
            [
                {
                    "code": "report_serialization_error",
                    "message": "internal proof report contained non-serializable content",
                    "location": "",
                }
            ]
        )


def _structural_response(errors: list[dict[str, str]]) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "status": "invalid_manifest",
            "serviceable": False,
            "errors": sorted(errors, key=lambda item: (item.get("location", ""), item.get("code", ""), item.get("message", ""))),
        },
    )
