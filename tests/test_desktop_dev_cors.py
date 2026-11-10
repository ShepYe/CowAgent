"""真实 WSGI 响应：调试跨域、预检与原接口认证。"""
import pytest
import web

from channel.web.core.desktop_dev import desktop_dev_cors


class Endpoint:
    def GET(self):
        if web.ctx.env.get('HTTP_AUTHORIZATION') != 'Bearer local-test':
            raise web.unauthorized()
        # SSE handlers can set this; the processor must replace it uniquely.
        web.header('Access-Control-Allow-Origin', '*')
        return 'ok'

    POST = GET
    PUT = GET
    DELETE = GET


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv('COW_DESKTOP', '1')
    monkeypatch.setenv('COW_DESKTOP_RENDERER_MODE', 'dev')
    result = web.application(('/', 'Endpoint'), globals(), autoreload=False)
    result.add_processor(desktop_dev_cors)
    return result


def request(app, method='GET', origin='http://localhost:5173', ip='127.0.0.1', auth=False, requested_method=None):
    headers = {'Origin': origin}
    if requested_method:
        headers['Access-Control-Request-Method'] = requested_method
    if auth:
        headers['Authorization'] = 'Bearer local-test'
    return app.request('/', method=method, headers=headers, env={'REMOTE_ADDR': ip})


@pytest.mark.parametrize('method', ['GET', 'POST', 'PUT', 'DELETE'])
def test_preflight_and_authenticated_response(app, method):
    response = request(app, 'OPTIONS', requested_method=method)
    assert response.status == '204 No Content'
    assert response.headers['Access-Control-Allow-Origin'] == 'http://localhost:5173'
    assert 'X-Cow-Desktop-Token' in response.headers['Access-Control-Allow-Headers']
    assert method in {value.strip() for value in response.headers['Access-Control-Allow-Methods'].split(',')}
    response = request(app, method, auth=True)
    assert response.data == b'ok'
    assert response.headers['Access-Control-Allow-Origin'] == 'http://localhost:5173'
    assert response.headers['Access-Control-Allow-Credentials'] == 'true'


def test_dev_origin_still_requires_endpoint_authentication(app):
    response = request(app)
    assert response.status.startswith('401')
    assert response.headers['Access-Control-Allow-Origin'] == 'http://localhost:5173'


@pytest.mark.parametrize('origin,ip', [('https://example.com', '127.0.0.1'), ('http://localhost:5173', '10.0.0.2')])
def test_other_origins_and_remote_clients_do_not_get_preflight(app, origin, ip):
    response = request(app, 'OPTIONS', origin, ip)
    assert response.status.startswith('405')
    assert 'Access-Control-Allow-Origin' not in response.headers


@pytest.mark.parametrize('name,value', [('COW_DESKTOP', '0'), ('COW_DESKTOP_RENDERER_MODE', 'built')])
def test_regular_runtime_does_not_enable_dev_cors(app, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    response = request(app, 'OPTIONS')
    assert response.status.startswith('405')
    assert 'Access-Control-Allow-Origin' not in response.headers
