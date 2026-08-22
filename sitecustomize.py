"""Project-local Python startup hook.

Python imports ``sitecustomize`` automatically (unless started with ``-S``).
Keep startup hardening centralized so watcher/helper/test entry points share the
same safety behavior.
"""

try:
    import relogin
    import relogin_patch

    relogin_patch.apply_patch(relogin)
except Exception:
    # Startup customization must never prevent Python from launching. Any
    # failure remains visible through the normal regression tests.
    pass

try:
    import sensitive_log_guard

    sensitive_log_guard.install()
except Exception:
    # Logging hardening is defense in depth and must never block startup.
    pass
