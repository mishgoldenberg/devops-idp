## What this changes

<!-- What was wrong, and what you changed. One or two sentences is fine. -->

## How you checked it

<!--
Say what you actually ran. "Tested locally" is hard to act on; "opened the
dashboard with SonarQube disconnected and the widget showed the reason" is not.
If you tested against a mock rather than a real system, say so - that is fine,
it just changes what a reviewer looks at.
-->

## Screenshots

<!-- Required if anything visual changed. Before and after if you have both. -->

## Checklist

- [ ] The six guards in `scripts/` pass (`check_imports`, `check_css_classes`, `check_inline_js`, `check_nginx_sync`, `check_requester_fields`, `check_changelog`)
- [ ] There is a `backend/app/changelog.py` entry, written from the user's side - the symptom they saw, not the function I fixed
- [ ] No new CSS class that is missing from `frontend/static/css/output.css` (or `output.css` is rebuilt and committed)
- [ ] No integration credential is returned to the frontend or written to a log
- [ ] Any list two layers must agree on lives in one module both import
