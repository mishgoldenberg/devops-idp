from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

ui_router = APIRouter()


def _get_templates(request: Request):
    """Retrieve the Jinja2Templates instance stored on the app state."""
    return request.app.state.templates  # type: ignore


@ui_router.get("/ui/", response_class=HTMLResponse)
def ui_index(request: Request):
    """Render the HTMX-based UI landing page."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/hello", response_class=HTMLResponse)
def ui_hello(request: Request, name: str = "world"):
    """Return a small HTML fragment that can be fetched via HTMX."""
    templates = _get_templates(request)

    return templates.TemplateResponse(
        "partials/hello.html",
        {
            "request": request,
            "name": name,
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )
