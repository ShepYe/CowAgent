"""仅为显式启用的本机 Electron/Vite 调试提供接口跨域响应。"""
import os

import web


def desktop_dev_cors(handle):
    origin = web.ctx.env.get('HTTP_ORIGIN', '')
    if not (os.environ.get('COW_DESKTOP') == '1'
            and os.environ.get('COW_DESKTOP_RENDERER_MODE') == 'dev'
            and origin == 'http://localhost:5173'
            and web.ctx.env.get('REMOTE_ADDR') in {'127.0.0.1', '::1'}):
        return handle()
    try:
        if web.ctx.method == 'OPTIONS':
            web.ctx.status = '204 No Content'
            return ''
        return handle()
    finally:
        web.ctx.headers = [(name, value) for name, value in web.ctx.headers
                           if name.lower() != 'access-control-allow-origin']
        web.header('Access-Control-Allow-Origin', origin, unique=True)
        web.header('Access-Control-Allow-Credentials', 'true', unique=True)
        web.header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Cow-Desktop-Token', unique=True)
        web.header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS', unique=True)
        web.header('Vary', 'Origin', unique=True)
