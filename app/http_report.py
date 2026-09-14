"""HTTP reporting layer."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .parser import ManifestError, parse_yaml
from .proof import analyze_graph
from .topology import TopologyBuildError, build_graph


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
    except ManifestError as exc:
        return _structural_response(
            [
                {"code": issue.code, "message": issue.message, "location": issue.location}
                for issue in exc.issues
            ]
        )
    except TopologyBuildError as exc:
        # This normally represents a parser invariant failure; malformed input must still be 422.
        return _structural_response([{"code": "topology_build_error", "message": str(exc), "location": ""}])

    report = analyze_graph(graph)
    # Topology conflicts are still a normal protocol response with failure evidence.
    return JSONResponse(status_code=200, content=report)


def _structural_response(errors: list[dict[str, str]]) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "status": "invalid_manifest",
            "serviceable": False,
            "errors": sorted(errors, key=lambda item: (item.get("location", ""), item.get("code", ""), item.get("message", ""))),
        },
    )
