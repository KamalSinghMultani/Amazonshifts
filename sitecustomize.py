"""Project-local Python startup hook.

Python imports ``sitecustomize`` automatically (unless started with ``-S``).
Keeping the two focused relogin fixes here means they apply consistently to:

* watcher_v3.py;
* session_refresh.py subprocesses;
* doctor/tests;
* direct module invocations.

This is intentionally tiny. The implementation lives in relogin_patch.py so
there is one source of truth for the corrected behavior.
"""

try:
    import relogin
    import relogin_patch

    relogin_patch.apply_patch(relogin)
except Exception:
    # Startup customization must never prevent Python from launching. Any
    # failure remains visible through the normal regression tests.
    pass
