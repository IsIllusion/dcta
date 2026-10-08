"""Render entry point using the provider's assigned HTTPS origin."""
import os
from urllib.parse import urlsplit

from .deployment_check import check_environment
from .public_server import main


def configure_render():
    if os.environ.get('RENDER') != 'true':
        raise ValueError('This entry point runs on Render. Use dcta.public_server for local preview.')
    assigned = os.environ.get('RENDER_EXTERNAL_URL', '')
    url = urlsplit(assigned)
    if (url.scheme != 'https' or not url.hostname or not url.hostname.endswith('.onrender.com')
            or url.username or url.password or url.port or url.path or url.query or url.fragment):
        raise ValueError('Render has not provided a valid HTTPS service address.')
    # Never derive an allowed host from request headers.
    origin = os.environ.get('PUBLIC_ORIGIN') or assigned
    os.environ['PUBLIC_ORIGIN'] = origin
    os.environ['DEMO_DOMAIN'] = urlsplit(origin).hostname or ''
    defaults = {'DEPLOYMENT_SLOT': 'render-free', 'BIND_ADDRESS': '0.0.0.0', 'PORT': '10000',
                'DCTA_DATA_DIR': '/data', 'MAX_VISITORS': '12', 'HTTP_WORKERS': '4',
                'VOICE_WORKERS': '1', 'VISITOR_AI_REQUESTS': '60', 'DAILY_AI_REQUESTS': '300'}
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


if __name__ == '__main__':
    configure_render()
    check_environment(fresh_data=os.environ.get('DCTA_REQUIRE_FRESH_DATA', '1') == '1')
    main()
