"""Small product adapters: official scope, identity and vendor-specific parsing."""
from .nvidia import NvidiaProductAdapter
from .supermicro import SupermicroProductAdapter


def adapter_for(company_id, known_catalog=None):
    classes = {'nvidia': NvidiaProductAdapter, 'supermicro': SupermicroProductAdapter}
    if company_id not in classes:
        raise ValueError('unsupported product adapter: ' + company_id)
    return classes[company_id](known_catalog=known_catalog)
