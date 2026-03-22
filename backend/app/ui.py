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


@ui_router.get("/ui/components/quick-links", response_class=HTMLResponse)
def ui_quick_links_component(request: Request):
    """Render the Quick Links dashboard component for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/quick-links.html",
        {
            "request": request,
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )
