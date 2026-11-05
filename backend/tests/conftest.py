"""Keep local runtime credentials out of all regression tests."""

import pytest


def pytest_addoption(parser):
    parser.addoption('--offline-contracts',action='store_true',default=False,
        help='Deny Python socket connections in the bounded pipeline contract suite.')


@pytest.fixture(autouse=True)
def offline_contract_transport_guard(request, monkeypatch):
    if not request.config.getoption('--offline-contracts'):
        return
    def prohibited(*args, **kwargs):
        raise AssertionError('Network connections are disabled for offline pipeline contracts')
    monkeypatch.setattr('socket.create_connection',prohibited)
    monkeypatch.setattr('socket.socket.connect',prohibited)
    monkeypatch.setattr('socket.socket.connect_ex',prohibited)


@pytest.fixture(autouse=True, scope="session")
def isolated_project_env(tmp_path_factory):
    # Production .env intentionally takes precedence over process variables.
    # Tests inject their own environment/context and must never read that file.
    location = tmp_path_factory.mktemp("isolated-settings") / ".env"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("audit_settings._project_env_path", lambda: location)
        yield
