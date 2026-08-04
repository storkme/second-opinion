import os
import tempfile

# Never let the provider registration write into the developer's real ~/.pi config during
# tests (providers.write_models_json is called by any code path exercising the model
# registration). Redirect the write target to a scratch file: PROVIDER/env defaults still
# apply, only the destination moves.
os.environ.setdefault(
    "PI_MODELS_PATH",
    os.path.join(tempfile.gettempdir(), "second-opinion-test-models.json"),
)
