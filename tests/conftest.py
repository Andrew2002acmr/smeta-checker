"""Keep local runtime credentials out of all regression tests."""

import pytest


@pytest.fixture(autouse=True, scope="session")
def isolated_project_env(tmp_path_factory):
    # Production .env intentionally takes precedence over process variables.
    # Tests inject their own environment/context and must never read that file.
    location = tmp_path_factory.mktemp("isolated-settings") / ".env"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("audit_settings._project_env_path", lambda: location)
        yield
