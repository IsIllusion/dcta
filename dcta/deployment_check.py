"""Validate deployment configuration and the initial empty volume without API calls."""
import argparse
import json
import os
from pathlib import Path
import re

from .domain import DomainError
from .public_server import Settings, application_factory


def valid_domain(domain):
    return (len(domain) <= 253 and '.' in domain and
            all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in domain.split('.')) and
            not domain.endswith(('.example', '.invalid', '.test', '.localhost')) and
            domain != 'example.com' and not domain.endswith('.example.com') and
            not re.fullmatch(r'[0-9.]+', domain))


def require_empty_data(path):
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError('Fresh launch requires a separate data volume.')
    if candidate.exists() and (not candidate.is_dir() or any(candidate.iterdir())):
        raise ValueError('The launch volume is not empty. Use a new DEPLOYMENT_SLOT for the first public launch.')


def check_environment(fresh_data=False):
    domain = os.environ.get('DEMO_DOMAIN', '')
    if not valid_domain(domain):
        raise ValueError('Set DEMO_DOMAIN to the purchased hostname.')
    origin = os.environ.get('PUBLIC_ORIGIN', '')
    if origin != 'https://' + domain:
        raise ValueError('PUBLIC_ORIGIN must be https:// followed by DEMO_DOMAIN.')
    slot = os.environ.get('DEPLOYMENT_SLOT', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', slot):
        raise ValueError('DEPLOYMENT_SLOT must contain lowercase letters, digits and hyphens.')
    data_dir = Path(os.environ.get('DCTA_DATA_DIR', '/data'))
    Settings(origin, data_dir,
        visitor_ttl=int(os.environ.get('VISITOR_TTL_SECONDS', '86400')),
        max_visitors=int(os.environ.get('MAX_VISITORS', '100')),
        visitor_ai_requests=int(os.environ.get('VISITOR_AI_REQUESTS', '120')),
        daily_ai_requests=int(os.environ.get('DAILY_AI_REQUESTS', '2000')),
        http_workers=int(os.environ.get('HTTP_WORKERS', '16')))
    application_factory()  # Credential format and SDK checks only; no visitor or API request.
    if fresh_data:
        require_empty_data(data_dir)
    return {'configuration': 'ready', 'initial_volume': 'empty' if fresh_data else 'not_checked',
            'provider_requests_made': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fresh-data', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(check_environment(args.fresh_data)))
    except (DomainError, ValueError) as exc:
        print(json.dumps({'configuration': 'incomplete', 'message': str(exc)}))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
