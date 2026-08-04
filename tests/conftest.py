import os
import tempfile

# The reasoning-burn fallback (run_pass -> _model_reasoning -> write_models_json) rewrites
# models.json mid-test. Never point that at the developer's real ~/.pi config: redirect to a
# scratch file. (PROVIDER/env defaults still apply; only the write target moves.)
os.environ.setdefault(
    "PI_MODELS_PATH",
    os.path.join(tempfile.gettempdir(), "second-opinion-test-models.json"),
)
